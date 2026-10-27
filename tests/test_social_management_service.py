"""Routed group/guild management, ignores, and invitation authorization."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from dso_server.persistence.social_state import (
    GUILD_RANK_OFFICER,
    SocialStateStore,
    make_social_member,
)
from dso_server.protocol.character_selection import BitWriter
from dso_server.protocol.player_init_protocol import (
    NOTIFICATION_COMMAND as CLIENT_NOTIFICATION_COMMAND,
    CURRENCY_CHANGED_COMMAND,
    CurrencyChanged,
    decode_server_command_batch,
)
from dso_server.protocol.social_protocol import (
    BUDDY_LIST_COMMAND,
    GROUP_CANDIDATES_COMMAND,
    GROUP_CANDIDATES_RESULT_COMMAND,
    GROUP_EDIT_COMMAND,
    GROUP_EDIT_RESULT_COMMAND,
    GROUP_INVITATION_RESPONSE_COMMAND,
    GROUP_INVITE_COMMAND,
    GROUP_JOIN_REQUEST_COMMAND,
    GROUP_JOIN_RESPONSE_COMMAND,
    GROUP_KICK_COMMAND,
    GROUP_MULTIPLE_INVITE_COMMAND,
    GROUP_STATUS_COMMAND,
    GROUPING_FAILED_COMMAND,
    GUILD_INVITATION_RESPONSE_COMMAND,
    GUILD_INVITE_COMMAND,
    GUILD_KICK_COMMAND,
    GUILD_MOTD_CHANGE_COMMAND,
    GUILD_MOTD_COMMAND,
    GUILD_MOTD_REQUEST_COMMAND,
    GUILD_NAME_EDIT_COMMAND,
    GUILD_NAME_EDIT_RESULT_COMMAND,
    GUILD_STATUS_COMMAND,
    IGNORED_LIST_COMMAND,
    IGNORED_STATUS_COMMAND,
    decode_group_candidates,
    decode_group_edit_result,
    decode_group_join_response,
    decode_group_status,
    decode_grouping_failed,
    decode_guild_motd,
    decode_guild_name_edit_result,
    decode_guild_status,
    decode_ignored_status,
    decode_social_kick_notice,
)
from dso_server.runtime.transport import SessionRegistry
from dso_server.services.character_server import CharacterServer
from dso_server.services.trade import TradeService
from dso_server.tests.test_pvp_matchmaking import _session


def request(command: int, *fields) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(command.to_bytes(2, "little"))
    for kind, value in fields:
        if kind == "u8":
            writer.write_uint8(value)
        elif kind == "u32":
            writer.write_uint32_le(value)
        elif kind == "str":
            writer.write_string(value)
        elif kind == "bool":
            writer.write_bool(value)
        elif kind == "str[]":
            writer.write_uint32_le(len(value))
            for item in value:
                writer.write_string(item)
        elif kind == "u32[]":
            writer.write_uint32_le(len(value))
            for item in value:
                writer.write_uint32_le(item)
    return writer.finish()


@pytest.fixture
def world(tmp_path):
    store = SocialStateStore(tmp_path / "social.json")
    server = CharacterServer.__new__(CharacterServer)
    server.service_name = "social-management-test"
    server.social_store = store
    server.sessions = SessionRegistry(threading.Lock())
    refreshed = []
    server.social_chat_callback = refreshed.append

    def connect(character_id, name, *, map_id="a0200_kingscity"):
        session = _session(52000 + character_id, character_id, name)
        session.gameplay_map = map_id
        session.gameplay_initialization_sent = True
        session.gameplay_presence_ready = True
        server.sessions[session.addr] = session
        return session

    def send(session, packet):
        payload, bits = packet
        session.current_message_bit_length = bits
        server._on_char_op(session, payload)

    yield SimpleNamespace(
        server=server, store=store, connect=connect, send=send, refreshed=refreshed,
    )
    store.close()


def member(character_id, name):
    return make_social_member(character_id, name, 1, 50)


def packets(session, command):
    return [
        (data, bits) for data, bits, _label in session.recorder.messages
        if len(data) >= 3 and int.from_bytes(data[1:3], "little") == command
    ]


def last(session, command):
    found = packets(session, command)
    assert found, f"no 0x{command:04x} sent"
    return found[-1]


def notifications(session):
    entries = []
    for data, bits in packets(session, CLIENT_NOTIFICATION_COMMAND):
        for command in decode_server_command_batch(data, bits):
            if command.command_id == CLIENT_NOTIFICATION_COMMAND:
                entries.extend(command.value.entries)
    return entries


def make_guild(world):
    store = world.store
    guild = store.create_guild("Iron Wolves", "", member(1, "Leader"))
    for character_id, name in ((2, "Officer"), (3, "Member"), (4, "Other")):
        store.add_guild_member(guild.guild_id, 1, member(character_id, name))
    with store.lock:
        stored = store.guilds[guild.guild_id]
        next(m for m in stored.members if m.character_id == 2).rank = GUILD_RANK_OFFICER
    store._queue_write_locked()
    store.flush()
    return guild.guild_id


def make_group(world, *members_, name="Party"):
    store = world.store
    leader_id, leader_name = members_[0]
    group = store.create_group(name, "", "a0200_kingscity", member(leader_id, leader_name))
    for character_id, member_name in members_[1:]:
        store.add_group_member(group.group_id, leader_id, member(character_id, member_name))
    return group.group_id


# -- kicks ------------------------------------------------------------------------

def test_guild_kick_by_an_officer_notifies_clears_and_broadcasts(world):
    guild_id = make_guild(world)
    officer = world.connect(2, "Officer")
    kicked = world.connect(3, "Member")
    leader = world.connect(1, "Leader")
    world.send(officer, request(GUILD_KICK_COMMAND, ("str", "member"), ("u32", 0), ("u32", 0)))
    notice, actor_id = decode_social_kick_notice(*last(kicked, GUILD_KICK_COMMAND), GUILD_KICK_COMMAND)
    assert notice.target_name == "Member" and actor_id
    cleared = decode_guild_status(*last(kicked, GUILD_STATUS_COMMAND))
    assert cleared.guild_id == 0 and cleared.members == ()
    for session in (officer, leader):
        roster = decode_guild_status(*last(session, GUILD_STATUS_COMMAND))
        assert roster.guild_id == guild_id
        assert {m.player_id for m in roster.members} == {1, 2, 4}
    assert (3,) in world.refreshed
    assert SocialStateStore(world.store.path).guild_for(3) is None


def test_a_rank_violation_is_refused_and_the_roster_resynchronised(world):
    make_guild(world)
    plain = world.connect(3, "Member")
    world.send(plain, request(GUILD_KICK_COMMAND, ("str", "Other"), ("u32", 0), ("u32", 0)))
    assert not packets(plain, GUILD_KICK_COMMAND)
    roster = decode_guild_status(*last(plain, GUILD_STATUS_COMMAND))
    assert {m.player_id for m in roster.members} == {1, 2, 3, 4}
    assert world.store.guild_for(4) is not None


def test_group_kick_is_leader_only_and_clears_the_kicked_status(world):
    group_id = make_group(world, (1, "Leader"), (2, "Two"), (3, "Three"))
    leader = world.connect(1, "Leader")
    two = world.connect(2, "Two")
    three = world.connect(3, "Three")
    world.send(two, request(GROUP_KICK_COMMAND, ("str", "Three"), ("u32", 0), ("u32", 0)))
    assert world.store.group_for(3).group_id == group_id
    world.send(leader, request(GROUP_KICK_COMMAND, ("str", "Three"), ("u32", 0), ("u32", 0)))
    assert packets(three, GROUP_KICK_COMMAND)
    assert decode_group_status(*last(three, GROUP_STATUS_COMMAND)).group_id == 0
    status = decode_group_status(*last(two, GROUP_STATUS_COMMAND))
    assert [m.character_id for m in status.members] == [1, 2]
    assert status.leader_rights is False
    assert decode_group_status(*last(leader, GROUP_STATUS_COMMAND)).leader_rights is True


# -- group edit -------------------------------------------------------------------

def test_group_edit_announces_the_new_name_before_the_roster(world):
    group_id = make_group(world, (1, "Leader"), (2, "Two"))
    leader = world.connect(1, "Leader")
    two = world.connect(2, "Two")
    world.send(leader, request(
        GROUP_EDIT_COMMAND, ("u32", 0xFFFFFFFF), ("str", "Leader"), ("str", "Night raid"), ("bool", True)
    ))
    for session in (leader, two):
        labels = [label for _d, _b, label in session.recorder.messages]
        assert labels.index("group-edit-result") < max(
            i for i, label in enumerate(labels) if label.startswith("group-status")
        )
        status, _field, echoed = decode_group_edit_result(*last(session, GROUP_EDIT_RESULT_COMMAND))
        assert (status, echoed.name, echoed.flag) == (0, "Night raid", True)
        roster = decode_group_status(*last(session, GROUP_STATUS_COMMAND))
        assert (roster.name, roster.private) == ("Night raid", True)
    assert world.store.groups_in_map("a0200_kingscity") == ()
    assert SocialStateStore(world.store.path).group_by_id(group_id).name == "Night raid"


def test_group_edit_with_an_empty_name_only_answers_the_editor(world):
    make_group(world, (1, "Leader"), (2, "Two"))
    leader = world.connect(1, "Leader")
    two = world.connect(2, "Two")
    world.send(leader, request(
        GROUP_EDIT_COMMAND, ("u32", 0), ("str", "Leader"), ("str", "   "), ("bool", False)
    ))
    status, _field, _echo = decode_group_edit_result(*last(leader, GROUP_EDIT_RESULT_COMMAND))
    assert status == 1
    assert not packets(two, GROUP_EDIT_RESULT_COMMAND)
    assert world.store.group_for(1).name == "Party"


def test_a_member_cannot_edit_the_group(world):
    make_group(world, (1, "Leader"), (2, "Two"))
    two = world.connect(2, "Two")
    world.send(two, request(
        GROUP_EDIT_COMMAND, ("u32", 0), ("str", "Two"), ("str", "Mine"), ("bool", False)
    ))
    assert not packets(two, GROUP_EDIT_RESULT_COMMAND)
    assert world.store.group_for(1).name == "Party"


# -- guild rename ---------------------------------------------------------------------

def wallet(session, *, andermant=0, gold=0):
    session.gameplay_currency_state = CurrencyChanged(andermant, gold, (0, 0, 0), 0, False)


def rename(world, session, name, *, andermant):
    world.send(session, request(
        GUILD_NAME_EDIT_COMMAND, ("u32", 0), ("str", ""), ("str", name), ("bool", andermant)
    ))


def test_guild_rename_charges_the_authored_silver_price(world):
    guild_id = make_guild(world)
    leader = world.connect(1, "Leader")
    member_session = world.connect(3, "Member")
    wallet(leader, andermant=5000, gold=3_500_000)
    rename(world, leader, "Silver Hawks", andermant=False)
    # _Globals.GuildEditNameVCCost = 3000000 copper.
    assert leader.gameplay_currency_state.gold == 500_000
    assert leader.gameplay_currency_state.andermant == 5000
    assert packets(leader, CURRENCY_CHANGED_COMMAND)
    for session in (leader, member_session):
        status, *_rest, name, _flag = decode_guild_name_edit_result(
            *last(session, GUILD_NAME_EDIT_RESULT_COMMAND)
        )
        assert (status, name) == (0, "Silver Hawks")
        assert decode_guild_status(*last(session, GUILD_STATUS_COMMAND)).name == "Silver Hawks"
    assert SocialStateStore(world.store.path).guild_for(4).name == "Silver Hawks"
    assert world.store.guild_for(1).guild_id == guild_id


def test_guild_rename_with_andermant_charges_the_rc_price(world):
    make_guild(world)
    leader = world.connect(1, "Leader")
    wallet(leader, andermant=2500, gold=0)
    rename(world, leader, "Silver Hawks", andermant=True)
    # _Globals.GuildEditNameRCCost = 2000 Andermant.
    assert leader.gameplay_currency_state.andermant == 500
    assert world.store.guild_for(1).name == "Silver Hawks"


@pytest.mark.parametrize("name,funds,expected", [
    ("Silver Hawks", 1999, 1),  # notEnoughFundsForEditingGuild
    ("Taken Name", 5000, 2),  # guildNameAlreadyAssigned
    ("ab", 5000, 3),  # guildNameInvalid
    ("Iron Wolves", 5000, 4),  # noNameChange
])
def test_failed_renames_change_nothing_and_charge_nothing(world, name, funds, expected):
    make_guild(world)
    world.store.create_guild("Taken Name", "", member(50, "Someone"))
    leader = world.connect(1, "Leader")
    other = world.connect(3, "Member")
    wallet(leader, andermant=funds)
    rename(world, leader, name, andermant=True)
    status = decode_guild_name_edit_result(*last(leader, GUILD_NAME_EDIT_RESULT_COMMAND))[0]
    assert status == expected
    assert leader.gameplay_currency_state.andermant == funds
    assert world.store.guild_for(1).name == "Iron Wolves"
    assert not packets(other, GUILD_NAME_EDIT_RESULT_COMMAND)


def test_only_the_guild_leader_can_rename(world):
    make_guild(world)
    officer = world.connect(2, "Officer")
    wallet(officer, andermant=5000)
    rename(world, officer, "Coup", andermant=True)
    assert decode_guild_name_edit_result(*last(officer, GUILD_NAME_EDIT_RESULT_COMMAND))[0] == 4
    assert officer.gameplay_currency_state.andermant == 5000


def test_a_rename_that_loses_the_race_refunds_the_fee(world, monkeypatch):
    make_guild(world)
    leader = world.connect(1, "Leader")
    wallet(leader, andermant=5000)
    store = world.store
    original = store.rename_guild

    def racing(actor, name):
        store.create_guild(name, "", member(77, "Faster"))
        return original(actor, name)

    monkeypatch.setattr(store, "rename_guild", racing)
    rename(world, leader, "Contested", andermant=True)
    assert decode_guild_name_edit_result(*last(leader, GUILD_NAME_EDIT_RESULT_COMMAND))[0] == 2
    assert leader.gameplay_currency_state.andermant == 5000
    assert store.guild_for(1).name == "Iron Wolves"


# -- message of the day -------------------------------------------------------------

def test_officer_message_is_broadcast_persisted_and_requested(world):
    make_guild(world)
    officer = world.connect(2, "Officer")
    plain = world.connect(3, "Member")
    world.send(officer, request(GUILD_MOTD_CHANGE_COMMAND, ("u32", 0), ("str", "Raid at 8")))
    for session in (officer, plain):
        assert decode_guild_motd(*last(session, GUILD_MOTD_COMMAND))[0] == "Raid at 8"
    plain.recorder.messages.clear()
    world.send(plain, request(GUILD_MOTD_REQUEST_COMMAND, ("u32", 0xFFFFFFFF)))
    assert decode_guild_motd(*last(plain, GUILD_MOTD_COMMAND))[0] == "Raid at 8"
    assert SocialStateStore(world.store.path).guild_for(3).message_of_the_day == "Raid at 8"


def test_members_cannot_change_and_deleting_sends_nothing(world):
    make_guild(world)
    leader = world.connect(1, "Leader")
    plain = world.connect(3, "Member")
    world.send(plain, request(GUILD_MOTD_CHANGE_COMMAND, ("u32", 0), ("str", "Spam")))
    assert not packets(leader, GUILD_MOTD_COMMAND)
    world.send(leader, request(GUILD_MOTD_CHANGE_COMMAND, ("u32", 0), ("str", "News")))
    leader.recorder.messages.clear()
    world.send(leader, request(GUILD_MOTD_CHANGE_COMMAND, ("u32", 0), ("str", "")))
    assert not packets(leader, GUILD_MOTD_COMMAND)
    assert world.store.guild_for(1).message_of_the_day == ""
    world.send(leader, request(GUILD_MOTD_REQUEST_COMMAND, ("u32", 0)))
    assert not packets(leader, GUILD_MOTD_COMMAND)


# -- ignore list ----------------------------------------------------------------------

def ignored_names(session):
    return decode_ignored_status(*last(session, IGNORED_STATUS_COMMAND))[2]


def test_ignore_add_remove_refresh_and_guild_status(world):
    make_guild(world)
    owner = world.connect(3, "Member")
    world.connect(9, "Pest")
    world.send(owner, request(IGNORED_LIST_COMMAND, ("u8", 1), ("u32", 0), ("str", "pest")))
    assert ignored_names(owner) == ("Pest",)
    assert SocialStateStore(world.store.path).ignored_for(3) == ((9, "Pest"),)
    # GuildStatus always replaces the client's list, so it must carry it.
    world.server._broadcast_social_status("guild", world.store.guild_for(3).guild_id)
    assert decode_guild_status(*last(owner, GUILD_STATUS_COMMAND)).ignored_names == ("Pest",)
    world.send(owner, request(IGNORED_LIST_COMMAND, ("u8", 0), ("u32", 0), ("str", "")))
    assert ignored_names(owner) == ("Pest",)
    world.send(owner, request(IGNORED_LIST_COMMAND, ("u8", 2), ("u32", 0), ("str", "PEST")))
    assert ignored_names(owner) == ()
    assert world.store.ignored_for(3) == ()


def test_ignoring_unknown_self_or_buddies_changes_nothing(world):
    owner = world.connect(3, "Member")
    world.connect(9, "Friend")
    world.store.add_buddy(3, 9)
    for name in ("Nobody", "Member", "Friend"):
        world.send(owner, request(IGNORED_LIST_COMMAND, ("u8", 1), ("u32", 0), ("str", name)))
        assert ignored_names(owner) == ()


def test_ignored_status_packet_for_login(world):
    world.store.set_ignored(3, 9, "Old name")
    world.connect(9, "New name")
    status, field_1c, names = decode_ignored_status(*world.server._ignored_status_packet(3))
    assert (status, field_1c, names) == (0, 3, ("New name",))


# -- ignores block invitations, buddies and trade ------------------------------------

def test_an_ignoring_target_never_sees_group_or_guild_invitations(world):
    make_guild(world)
    leader = world.connect(1, "Leader")
    target = world.connect(9, "Target")
    world.store.set_ignored(9, 1, "Leader")
    world.send(leader, request(GROUP_INVITE_COMMAND, ("str", "Leader"), ("str", "Target"), ("u32", 0)))
    world.send(leader, request(
        GUILD_INVITE_COMMAND, ("str", "Leader"), ("str", "Target"), ("u32", 0), ("u32", 0), ("str", "")
    ))
    assert notifications(target) == []
    for command in (GROUP_INVITATION_RESPONSE_COMMAND, GUILD_INVITATION_RESPONSE_COMMAND):
        assert last(leader, command)[0][3] == 1  # status: did not join


def test_ignored_players_cannot_become_buddies(world):
    owner = world.connect(3, "Owner")
    world.connect(9, "Pest")
    world.store.set_ignored(9, 3, "Owner")
    world.send(owner, request(
        BUDDY_LIST_COMMAND, ("u8", 1), ("u32", 0), ("str", "Pest"), ("bool", False)
    ))
    assert world.store.buddies_for(3) == ()


def test_trade_requests_respect_ignores(world, monkeypatch):
    from dso_server.services import trade as trade_module

    asker = world.connect(3, "Asker")
    target = world.connect(9, "Target")
    sent = []
    monkeypatch.setattr(TradeService, "_trade_sessions", staticmethod(
        lambda: iter(((world.server, asker), (world.server, target)))
    ))
    monkeypatch.setattr(TradeService, "_trade_capable", staticmethod(lambda s: True))
    monkeypatch.setattr(TradeService, "_trade_send", staticmethod(
        lambda s, state: sent.append((s, state))
    ))
    monkeypatch.setattr(trade_module, "_TRADES", {})
    world.store.set_ignored(9, 3, "Asker")
    world.server._trade_request(asker, 3, "Asker", "Target")
    assert sent and "not accepting" in sent[-1][1].message
    world.store.set_ignored(9, 3, "", remove=True)
    world.store.set_ignored(3, 9, "Target")
    world.server._trade_request(asker, 3, "Asker", "Target")
    assert "you are ignoring" in sent[-1][1].message
    assert trade_module._TRADES == {}


# -- invitation ledger authorization -------------------------------------------------

def invitation_response(command, status, inviter_id, collective_id, name):
    return request(command, ("u8", status), ("u32", inviter_id), ("u32", collective_id), ("str", name))


def test_a_forged_group_acceptance_admits_nobody_and_never_reaches_the_inviter(world):
    group_id = make_group(world, (1, "Leader"))
    leader = world.connect(1, "Leader")
    intruder = world.connect(9, "Intruder")
    world.send(intruder, invitation_response(GROUP_INVITATION_RESPONSE_COMMAND, 0, 1, group_id, "Intruder"))
    assert world.store.group_for(9) is None
    assert not packets(leader, GROUP_INVITATION_RESPONSE_COMMAND)
    assert decode_grouping_failed(*last(intruder, GROUPING_FAILED_COMMAND))[0] == 2


def test_a_forged_guild_acceptance_admits_nobody(world):
    guild_id = make_guild(world)
    intruder = world.connect(9, "Intruder")
    world.send(intruder, invitation_response(GUILD_INVITATION_RESPONSE_COMMAND, 0, 1, guild_id, "Intruder"))
    assert world.store.guild_for(9) is None


def test_a_real_invitation_admits_once_and_a_replay_is_harmless(world):
    leader = world.connect(1, "Leader")
    target = world.connect(9, "Target")
    world.send(leader, request(GROUP_INVITE_COMMAND, ("str", "Leader"), ("str", "Target"), ("u32", 0)))
    (entry,) = notifications(target)
    payload = json.loads(entry.payload)
    assert entry.kind == 1 and entry.values[2] == 1
    group_id = payload["collectiveId"]
    accept = invitation_response(GROUP_INVITATION_RESPONSE_COMMAND, 0, 1, group_id, "Target")
    world.send(target, accept)
    assert world.store.group_for(9).group_id == group_id
    assert last(leader, GROUP_INVITATION_RESPONSE_COMMAND)[0][3] == 0
    responses = len(packets(leader, GROUP_INVITATION_RESPONSE_COMMAND))
    world.send(target, accept)  # the notification echo / a replay
    assert len(packets(leader, GROUP_INVITATION_RESPONSE_COMMAND)) == responses
    world.store.leave_group(9)
    world.send(target, accept)
    assert world.store.group_for(9) is None


def test_a_declined_invitation_cannot_be_accepted_afterwards(world):
    guild_id = make_guild(world)
    leader = world.connect(1, "Leader")
    target = world.connect(9, "Target")
    world.send(leader, request(
        GUILD_INVITE_COMMAND, ("str", "Leader"), ("str", "Target"), ("u32", 0), ("u32", 0), ("str", "")
    ))
    world.send(target, invitation_response(GUILD_INVITATION_RESPONSE_COMMAND, 1, 1, guild_id, "Target"))
    assert last(leader, GUILD_INVITATION_RESPONSE_COMMAND)[0][3] == 1
    world.send(target, invitation_response(GUILD_INVITATION_RESPONSE_COMMAND, 0, 1, guild_id, "Target"))
    assert world.store.guild_for(9) is None


def notification_action(subtype, entry_kind, payload, values=(0, 0, 0)):
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(CLIENT_NOTIFICATION_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(2)
    writer.write_uint8(subtype)
    writer.write_uint8(entry_kind)
    writer.write_bytes((16).to_bytes(2, "little"))
    writer.write_bytes(bytes(16))
    for value in values:
        writer.write_uint32_le(value)
    writer.write_string(json.dumps(payload))
    writer.write_string("0000-00-00 00:00:00")
    return writer.finish()


def test_a_forged_notification_echo_cannot_join_a_guild(world):
    guild_id = make_guild(world)
    intruder = world.connect(9, "Intruder")
    payload, _bits = notification_action(3, 3, {
        "invitation": "guild", "sourceCharacterId": 1, "targetCharacterId": 9,
        "collectiveId": guild_id, "invitationId": "forged",
    })
    world.server._handle_notification_action(intruder, payload)
    assert world.store.guild_for(9) is None


def test_a_genuine_notification_echo_is_completed_through_the_ledger(world):
    guild_id = make_guild(world)
    leader = world.connect(1, "Leader")
    target = world.connect(9, "Target")
    world.send(leader, request(
        GUILD_INVITE_COMMAND, ("str", "Leader"), ("str", "Target"), ("u32", 0), ("u32", 0), ("str", "")
    ))
    (entry,) = notifications(target)
    payload, _bits = notification_action(3, entry.kind, json.loads(entry.payload), entry.values)
    world.server._handle_notification_action(target, payload)
    assert world.store.guild_for(9).guild_id == guild_id
    assert last(leader, GUILD_INVITATION_RESPONSE_COMMAND)[0][3] == 0


# -- group join requests ---------------------------------------------------------------

def join_request(name, group_name, leave=False):
    return request(
        GROUP_JOIN_REQUEST_COMMAND, ("str", name), ("str", group_name), ("u32", 0), ("u32", 0), ("bool", leave)
    )


def join_answer(status, requester_id, group_id, leader_name, token=""):
    return request(
        GROUP_JOIN_RESPONSE_COMMAND, ("u8", status), ("u32", requester_id), ("u32", group_id),
        ("str", leader_name), ("str", token),
    )


def test_join_request_reaches_the_leader_and_acceptance_admits(world):
    group_id = make_group(world, (1, "Leader"), (2, "Two"))
    leader = world.connect(1, "Leader")
    two = world.connect(2, "Two")
    asker = world.connect(9, "Asker")
    world.send(asker, join_request("Asker", "party"))
    (entry,) = notifications(leader)
    payload = json.loads(entry.payload)
    assert entry.kind == 2
    assert entry.values == (1, group_id, 9)
    assert payload["playerName"] == "Asker" and payload["timeStamp"]
    world.send(leader, join_answer(0, 9, group_id, "Leader", payload["timeStamp"]))
    answer, _actor = decode_group_join_response(*last(asker, GROUP_JOIN_RESPONSE_COMMAND))
    assert answer.status == 0
    assert world.store.group_for(9).group_id == group_id
    assert {m.character_id for m in decode_group_status(*last(two, GROUP_STATUS_COMMAND)).members} == {1, 2, 9}
    assert (9,) in world.refreshed


def test_join_acceptance_needs_the_issued_token_and_is_single_use(world):
    group_id = make_group(world, (1, "Leader"))
    leader = world.connect(1, "Leader")
    asker = world.connect(9, "Asker")
    world.send(asker, join_request("Asker", "Party"))
    token = json.loads(notifications(leader)[0].payload)["timeStamp"]
    world.send(leader, join_answer(0, 9, group_id, "Leader", "forged"))
    assert world.store.group_for(9) is None
    world.send(leader, join_answer(0, 9, group_id, "Leader", token))
    assert world.store.group_for(9).group_id == group_id
    world.store.leave_group(9)
    world.send(leader, join_answer(0, 9, group_id, "Leader", token))
    assert world.store.group_for(9) is None


def test_a_member_cannot_admit_by_answering_someone_elses_request(world):
    group_id = make_group(world, (1, "Leader"), (2, "Two"))
    leader = world.connect(1, "Leader")
    two = world.connect(2, "Two")
    asker = world.connect(9, "Asker")
    world.send(asker, join_request("Asker", "Party"))
    token = json.loads(notifications(leader)[0].payload)["timeStamp"]
    world.send(two, join_answer(0, 9, group_id, "Two", token))
    assert world.store.group_for(9) is None


def test_join_refusals_are_forwarded_and_duplicates_pend(world):
    group_id = make_group(world, (1, "Leader"))
    leader = world.connect(1, "Leader")
    asker = world.connect(9, "Asker")
    world.send(asker, join_request("Asker", "Party"))
    world.send(asker, join_request("Asker", "Party"))
    assert decode_group_join_response(*last(asker, GROUP_JOIN_RESPONSE_COMMAND))[0].status == 11
    assert len(notifications(leader)) == 1
    world.send(leader, join_answer(9, 9, group_id, "Leader"))  # requests disabled
    assert decode_group_join_response(*last(asker, GROUP_JOIN_RESPONSE_COMMAND))[0].status == 9
    # The refusal consumed the request: a forged refusal is not forwarded.
    count = len(packets(asker, GROUP_JOIN_RESPONSE_COMMAND))
    world.send(leader, join_answer(1, 9, group_id, "Leader"))
    assert len(packets(asker, GROUP_JOIN_RESPONSE_COMMAND)) == count


@pytest.mark.parametrize("case,expected", [
    ("unknown", 8), ("grouped", 2), ("ignored", 1), ("offline", 4), ("hidden", 8),
])
def test_join_request_failures(world, case, expected):
    group_id = make_group(world, (1, "Leader"))
    if case != "offline":
        world.connect(1, "Leader")
    asker = world.connect(9, "Asker")
    if case == "grouped":
        make_group(world, (9, "Asker"), (8, "Pal"), name="Mine")
    if case == "ignored":
        world.store.set_ignored(1, 9, "Asker")
    if case == "hidden":
        world.store.edit_group(1, "Party", True)
    world.send(asker, join_request("Asker", "Nope" if case == "unknown" else "Party"))
    answer, _actor = decode_group_join_response(*last(asker, GROUP_JOIN_RESPONSE_COMMAND))
    assert answer.status == expected
    assert world.store.pending_invitation("group-request", 9, 1, group_id) is None


def test_join_request_with_leave_flag_switches_groups(world):
    target_id = make_group(world, (1, "Leader"))
    old_id = make_group(world, (9, "Asker"), (8, "Pal"), name="Old")
    leader = world.connect(1, "Leader")
    asker = world.connect(9, "Asker")
    pal = world.connect(8, "Pal")
    world.send(asker, join_request("Asker", "Party", leave=True))
    token = json.loads(notifications(leader)[0].payload)["timeStamp"]
    world.send(leader, join_answer(0, 9, target_id, "Leader", token))
    assert world.store.group_for(9).group_id == target_id
    old = decode_group_status(*last(pal, GROUP_STATUS_COMMAND))
    assert old.group_id == old_id and [m.character_id for m in old.members] == [8]


# -- multiple invitations and candidates --------------------------------------------

def test_multiple_invite_records_one_invitation_per_target(world):
    leader = world.connect(1, "Leader")
    targets = [world.connect(10 + index, f"Pal{index}") for index in range(3)]
    world.send(leader, request(
        GROUP_MULTIPLE_INVITE_COMMAND, ("str", "Leader"),
        ("str[]", ("Pal0", "Pal1", "pal1", "Pal2")), ("u32", 0),
    ))
    group_id = world.store.group_for(1).group_id
    for index, target in enumerate(targets):
        assert len(notifications(target)) == 1
        assert world.store.pending_invitation("group", 1, 10 + index, group_id) is not None


def test_multiple_invite_beyond_the_ui_limit_is_refused(world):
    leader = world.connect(1, "Leader")
    with pytest.raises(ValueError):
        world.send(leader, request(
            GROUP_MULTIPLE_INVITE_COMMAND, ("str", "Leader"),
            ("str[]", tuple(f"P{index}" for index in range(11))), ("u32", 0),
        ))
    assert world.store.group_for(1) is None


def test_candidates_list_nearby_or_friends_without_ignored_or_members(world):
    make_group(world, (1, "Leader"), (2, "Two"))
    me = world.connect(1, "Leader")
    world.connect(2, "Two")
    world.connect(3, "Near")
    world.connect(4, "Pest")
    world.connect(5, "Faraway", map_id="b1200_hub")
    make_group(world, (6, "Busy"), (7, "Busy2"), name="Other")
    world.connect(6, "Busy")
    world.store.set_ignored(1, 4, "Pest")
    world.store.add_buddy(1, 5)
    world.send(me, request(GROUP_CANDIDATES_COMMAND, ("u32", 77), ("bool", True), ("u32[]", ())))
    field, rows, actor_id = decode_group_candidates(*last(me, GROUP_CANDIDATES_RESULT_COMMAND))
    assert field == 77 and actor_id
    assert {row.character_id: row.group_invitable for row in rows} == {3: True, 6: False}
    world.send(me, request(GROUP_CANDIDATES_COMMAND, ("u32", 0), ("bool", False), ("u32[]", ())))
    _field, rows, _actor = decode_group_candidates(*last(me, GROUP_CANDIDATES_RESULT_COMMAND))
    assert [row.character_id for row in rows] == [5]


# -- guild foundation fee -------------------------------------------------------------

def found(world, session, name, *, andermant):
    from dso_server.protocol.social_protocol import CREATE_GUILD_COMMAND

    world.send(session, request(
        CREATE_GUILD_COMMAND, ("u32", 0), ("str", session.gameplay_character.name),
        ("str", name), ("bool", andermant),
    ))


def foundation_status(session):
    from dso_server.protocol.social_protocol import CREATE_GUILD_RESULT_COMMAND

    return last(session, CREATE_GUILD_RESULT_COMMAND)[0][3]


def test_guild_foundation_charges_the_authored_fee(world):
    silver = world.connect(1, "Silver")
    premium = world.connect(2, "Premium")
    wallet(silver, andermant=0, gold=8_000_000)
    wallet(premium, andermant=6000, gold=0)
    found(world, silver, "Silver Guild", andermant=False)
    found(world, premium, "Premium Guild", andermant=True)
    # _Globals.GuildFoundationVCCost = 7500000, GuildFoundationRCCost = 5000.
    assert foundation_status(silver) == 0 and silver.gameplay_currency_state.gold == 500_000
    assert foundation_status(premium) == 0 and premium.gameplay_currency_state.andermant == 1000
    assert world.store.guild_for(1).name == "Silver Guild"
    assert world.store.guild_for(2).name == "Premium Guild"


@pytest.mark.parametrize("case,expected", [("poor", 2), ("taken", 3), ("member", 1), ("short", 4)])
def test_rejected_foundations_charge_nothing(world, case, expected):
    founder = world.connect(1, "Founder")
    wallet(founder, andermant=4999 if case == "poor" else 9999)
    if case == "taken":
        world.store.create_guild("Wanted Name", "", member(50, "Someone"))
    if case == "member":
        world.store.create_guild("Old Guild", "", member(1, "Founder"))
    found(world, founder, "ab" if case == "short" else "Wanted Name", andermant=True)
    assert foundation_status(founder) == expected
    assert founder.gameplay_currency_state.andermant == (4999 if case == "poor" else 9999)
    assert world.store.guild_for(1) is None or world.store.guild_for(1).name == "Old Guild"


def test_leaving_refreshes_the_remaining_members_and_promotes_a_leader(world):
    from dso_server.protocol.social_protocol import GROUP_LEAVE_COMMAND, GUILD_LEAVE_COMMAND

    make_group(world, (1, "Leader"), (2, "Two"))
    make_guild(world)
    leader = world.connect(1, "Leader")
    two = world.connect(2, "Two")
    world.send(leader, request(GROUP_LEAVE_COMMAND, ("u32", 0)))
    status = decode_group_status(*last(two, GROUP_STATUS_COMMAND))
    assert status.owner_character_id == 2 and status.leader_rights is True
    assert [m.character_id for m in status.members] == [2]
    world.send(leader, request(GUILD_LEAVE_COMMAND, ("u32", 0)))
    roster = decode_guild_status(*last(two, GUILD_STATUS_COMMAND))
    assert {m.player_id: m.rank for m in roster.members}[2] == 2  # officer promoted
