"""Durable group/guild management, the invitation ledger, and ignores."""

from __future__ import annotations

import json

import pytest

from dso_server.persistence.social_state import (
    GROUP_MEMBER_LIMIT,
    GUILD_RANK_LEADER,
    GUILD_RANK_MEMBER,
    GUILD_RANK_OFFICER,
    IGNORE_LIST_LIMIT,
    INVITATION_TTL_SECONDS,
    GroupFullError,
    InvitationMissingError,
    InvitationPendingError,
    SocialContactBlockedError,
    SocialNameError,
    SocialNameTakenError,
    SocialNoChangeError,
    SocialPermissionError,
    SocialStateStore,
    SocialTargetError,
    make_social_member,
)


def member(character_id, name):
    return make_social_member(character_id, name, 1, 50)


@pytest.fixture
def store(tmp_path):
    value = SocialStateStore(tmp_path / "social.json")
    yield value
    value.close()


def reload(store):
    store.flush()
    return SocialStateStore(store.path)


def guild_with_ranks(store):
    guild = store.create_guild("Iron Wolves", "", member(1, "Leader"))
    for character_id, name in ((2, "Officer"), (3, "Member"), (4, "Other")):
        store.add_guild_member(guild.guild_id, 1, member(character_id, name))
    with store.lock:
        stored = store.guilds[guild.guild_id]
        next(m for m in stored.members if m.character_id == 2).rank = GUILD_RANK_OFFICER
    store._queue_write_locked()
    return guild.guild_id


# -- invitation ledger -------------------------------------------------------

def test_an_answer_without_an_issued_invitation_admits_nobody(store):
    group = store.create_group("Party", "", "a0200_kingscity", member(1, "Leader"))
    with pytest.raises(InvitationMissingError):
        store.answer_invitation("group", 1, 2, group.group_id, member(2, "Joiner"), accept=True)
    assert store.group_for(2) is None


def test_an_invitation_admits_once_and_a_replay_is_refused(store):
    group = store.create_group("Party", "", "a0200_kingscity", member(1, "Leader"))
    store.issue_invitation("group", 1, 2, group.group_id, "token-a")
    joined = store.answer_invitation(
        "group", 1, 2, group.group_id, member(2, "Joiner"), accept=True, token="token-a"
    )
    assert [m.character_id for m in joined.members] == [1, 2]
    store.leave_group(2)
    with pytest.raises(InvitationMissingError):
        store.answer_invitation("group", 1, 2, group.group_id, member(2, "Joiner"), accept=True)
    assert store.group_for(2) is None


def test_the_answer_must_come_from_the_invited_character_for_that_collection(store):
    group = store.create_group("Party", "", "a0200_kingscity", member(1, "Leader"))
    other = store.create_group("Other", "", "a0200_kingscity", member(5, "Else"))
    store.issue_invitation("group", 1, 2, group.group_id, "t")
    for source, target, collective in ((1, 3, group.group_id), (5, 2, other.group_id), (1, 2, other.group_id)):
        with pytest.raises(InvitationMissingError):
            store.answer_invitation(
                "group", source, target, collective, member(target, "X"), accept=True
            )
    # A different character cannot ride the invitation either.
    with pytest.raises(ValueError):
        store.answer_invitation("group", 1, 2, group.group_id, member(3, "Imposter"), accept=True)
    assert store.group_for(3) is None


def test_a_wrong_token_leaves_the_real_invitation_in_place(store):
    guild = store.create_guild("Iron Wolves", "", member(1, "Leader"))
    store.issue_invitation("guild", 1, 2, guild.guild_id, "real")
    with pytest.raises(InvitationMissingError):
        store.answer_invitation("guild", 1, 2, guild.guild_id, member(2, "J"), accept=True, token="fake")
    store.answer_invitation("guild", 1, 2, guild.guild_id, member(2, "J"), accept=True, token="real")
    assert store.guild_for(2).guild_id == guild.guild_id


def test_invitations_expire_and_duplicates_stay_pending(store):
    group = store.create_group("Party", "", "a0200_kingscity", member(1, "Leader"))
    store.issue_invitation("group", 1, 2, group.group_id, "t", now=100.0)
    with pytest.raises(InvitationPendingError):
        store.issue_invitation("group", 1, 2, group.group_id, "t2", now=101.0)
    with pytest.raises(InvitationMissingError):
        store.answer_invitation(
            "group", 1, 2, group.group_id, member(2, "J"), accept=True,
            now=100.0 + INVITATION_TTL_SECONDS,
        )
    # After expiry a fresh invitation can be issued.
    store.issue_invitation("group", 1, 2, group.group_id, "t3", now=200.0 + INVITATION_TTL_SECONDS)


def test_a_refusal_consumes_the_invitation(store):
    group = store.create_group("Party", "", "a0200_kingscity", member(1, "Leader"))
    store.issue_invitation("group", 1, 2, group.group_id, "t")
    assert store.answer_invitation("group", 1, 2, group.group_id, accept=False) is None
    with pytest.raises(InvitationMissingError):
        store.answer_invitation("group", 1, 2, group.group_id, member(2, "J"), accept=True)


def test_only_members_with_invite_rights_can_issue(store):
    guild_id = guild_with_ranks(store)
    with pytest.raises(SocialPermissionError):
        store.issue_invitation("guild", 3, 9, guild_id, "t")
    store.issue_invitation("guild", 2, 9, guild_id, "t")  # officers may invite
    group = store.create_group("Party", "", "a0200_kingscity", member(10, "Leader"))
    store.add_group_member(group.group_id, 10, member(11, "Plain"))
    with pytest.raises(SocialPermissionError):
        store.issue_invitation("group", 11, 12, group.group_id, "t")


def test_a_full_group_cannot_issue_or_admit(store):
    group = store.create_group("Party", "", "a0200_kingscity", member(1, "Leader"))
    store.issue_invitation("group", 1, 99, group.group_id, "late")
    for character_id in range(2, GROUP_MEMBER_LIMIT + 1):
        store.add_group_member(group.group_id, 1, member(character_id, f"M{character_id}"))
    with pytest.raises(GroupFullError):
        store.issue_invitation("group", 1, 98, group.group_id, "t")
    with pytest.raises(GroupFullError):
        store.answer_invitation("group", 1, 99, group.group_id, member(99, "Late"), accept=True)


def test_join_request_with_leave_flag_moves_the_requester(store):
    target = store.create_group("Target", "", "a0200_kingscity", member(1, "Leader"))
    old = store.create_group("Old", "", "a0200_kingscity", member(2, "Asker"))
    store.add_group_member(old.group_id, 2, member(3, "Friend"))
    store.issue_invitation("group-request", 2, 1, target.group_id, "tok", leave_current=True)
    store.answer_invitation(
        "group-request", 2, 1, target.group_id, member(2, "Asker"), accept=True, token="tok"
    )
    assert store.group_for(2).group_id == target.group_id
    remaining = store.group_for(3)
    assert remaining.group_id == old.group_id and remaining.owner_character_id == 3


def test_join_request_without_leave_flag_keeps_a_real_group(store):
    target = store.create_group("Target", "", "a0200_kingscity", member(1, "Leader"))
    old = store.create_group("Old", "", "a0200_kingscity", member(2, "Asker"))
    store.add_group_member(old.group_id, 2, member(3, "Friend"))
    store.issue_invitation("group-request", 2, 1, target.group_id, "tok")
    with pytest.raises(ValueError):
        store.answer_invitation(
            "group-request", 2, 1, target.group_id, member(2, "Asker"), accept=True, token="tok"
        )
    assert store.group_for(2).group_id == old.group_id


def test_a_singleton_group_is_left_when_accepting_an_invitation(store):
    target = store.create_group("Target", "", "a0200_kingscity", member(1, "Leader"))
    lonely = store.create_group("Lonely", "", "a0200_kingscity", member(2, "Solo"))
    store.issue_invitation("group", 1, 2, target.group_id, "t")
    store.answer_invitation("group", 1, 2, target.group_id, member(2, "Solo"), accept=True)
    assert store.group_by_id(lonely.group_id) is None
    assert store.group_for(2).group_id == target.group_id


# -- kicks ----------------------------------------------------------------------

def test_group_kick_is_leader_only(store):
    group = store.create_group("Party", "", "a0200_kingscity", member(1, "Leader"))
    store.add_group_member(group.group_id, 1, member(2, "Two"))
    store.add_group_member(group.group_id, 1, member(3, "Three"))
    with pytest.raises(SocialPermissionError):
        store.kick_member("group", 2, "Three")
    with pytest.raises(SocialTargetError):
        store.kick_member("group", 1, "Leader")
    with pytest.raises(SocialTargetError):
        store.kick_member("group", 1, "Nobody")
    collective_id, removed = store.kick_member("group", 1, "  two ")
    assert (collective_id, removed.character_id) == (group.group_id, 2)
    assert store.group_for(2) is None
    assert [m.character_id for m in reload(store).group_for(1).members] == [1, 3]


def test_guild_kick_requires_a_strictly_higher_rank(store):
    guild_id = guild_with_ranks(store)
    with pytest.raises(SocialPermissionError):
        store.kick_member("guild", 3, "Other")  # member -> member
    with pytest.raises(SocialPermissionError):
        store.kick_member("guild", 2, "Leader")  # officer -> leader
    store.kick_member("guild", 2, "Member")  # officer -> member
    store.kick_member("guild", 1, "Officer")  # leader -> officer
    reloaded = reload(store)
    assert {m.character_id for m in reloaded.guild_for(1).members} == {1, 4}
    assert reloaded.guild_for(2) is None and reloaded.guild_for(3) is None
    assert reloaded.guild_for(4).guild_id == guild_id


def test_a_kicked_inviter_loses_pending_invitations_for_that_collection(store):
    guild_id = guild_with_ranks(store)
    store.issue_invitation("guild", 2, 9, guild_id, "t")
    store.kick_member("guild", 1, "Officer")
    with pytest.raises(InvitationMissingError):
        store.answer_invitation("guild", 2, 9, guild_id, member(9, "New"), accept=True)


# -- group edit / guild rename / message of the day ---------------------------

def test_group_edit_renames_hides_and_persists(store):
    group = store.create_group("Party", "", "a0200_kingscity", member(1, "Leader"))
    store.add_group_member(group.group_id, 1, member(2, "Two"))
    with pytest.raises(SocialPermissionError):
        store.edit_group(2, "Mine", False)
    with pytest.raises(SocialNameError):
        store.edit_group(1, "   ", False)
    with pytest.raises(SocialNameError):
        store.edit_group(1, "x" * 41, False)
    edited = store.edit_group(1, "  Night raid ", True)
    assert (edited.name, edited.private, edited.listed) == ("Night raid", True, False)
    with pytest.raises(SocialNoChangeError):
        store.edit_group(1, "Night raid", True)
    assert store.groups_in_map("a0200_kingscity") == ()
    reloaded = reload(store).group_for(2)
    assert (reloaded.name, reloaded.private, reloaded.listed) == ("Night raid", True, False)


def test_guild_rename_validation_and_name_index(store):
    guild_id = guild_with_ranks(store)
    store.create_guild("Taken Name", "", member(20, "Someone"))
    with pytest.raises(SocialPermissionError):
        store.validate_guild_rename(2, "Fresh")
    with pytest.raises(SocialNameError):
        store.validate_guild_rename(1, "ab")
    with pytest.raises(SocialNoChangeError):
        store.validate_guild_rename(1, "Iron Wolves")
    with pytest.raises(SocialNameTakenError):
        store.validate_guild_rename(1, "taken name")
    # A case-only change of the own name is allowed.
    assert store.rename_guild(1, "IRON WOLVES").name == "IRON WOLVES"
    store.rename_guild(1, "Silver Hawks")
    # The old name is free again, the new one is reserved.
    store.create_guild("Iron Wolves", "", member(30, "Founder"))
    with pytest.raises(ValueError):
        store.create_guild("silver hawks", "", member(31, "Copycat"))
    reloaded = reload(store)
    assert reloaded.guild_for(4).name == "Silver Hawks"
    assert reloaded.guild_for(4).guild_id == guild_id
    with pytest.raises(SocialNameTakenError):
        reloaded.validate_guild_rename(1, "Iron Wolves")


def test_guild_message_is_for_officers_and_persists(store):
    guild_with_ranks(store)
    with pytest.raises(SocialPermissionError):
        store.change_guild_message(3, "hi")
    store.change_guild_message(2, "  Raid tonight  ")
    with pytest.raises(SocialNoChangeError):
        store.change_guild_message(1, "Raid tonight")
    with pytest.raises(ValueError):
        store.change_guild_message(1, "x" * 1025)
    assert reload(store).guild_for(3).message_of_the_day == "Raid tonight"
    store.change_guild_message(1, "")
    assert reload(store).guild_for(3).message_of_the_day == ""


def test_leader_departure_promotes_an_officer_before_older_members(store):
    guild_with_ranks(store)
    store.leave_guild(1)
    guild = reload(store).guild_for(3)
    ranks = {m.character_id: m.rank for m in guild.members}
    assert ranks == {2: GUILD_RANK_LEADER, 3: GUILD_RANK_MEMBER, 4: GUILD_RANK_MEMBER}


# -- ignores ----------------------------------------------------------------------

def test_ignores_persist_and_block_contact_both_ways(store):
    assert store.set_ignored(1, 2, "Pest") is True
    assert store.set_ignored(1, 2, "Pest") is False
    assert store.social_contact_blocked(2, 1) and store.social_contact_blocked(1, 2)
    assert store.ignores(1, 2) and not store.ignores(2, 1)
    reloaded = reload(store)
    assert reloaded.ignored_for(1) == ((2, "Pest"),)
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["ignored"] == {"1": {"2": "Pest"}}
    assert reloaded.set_ignored(1, 2, "", remove=True) is True
    assert reloaded.set_ignored(1, 2, "", remove=True) is False
    assert reload(reloaded).ignored_for(1) == ()


def test_ignoring_withdraws_pending_invitations_and_blocks_new_ones(store):
    group = store.create_group("Party", "", "a0200_kingscity", member(1, "Leader"))
    store.issue_invitation("group", 1, 2, group.group_id, "t")
    store.set_ignored(2, 1, "Leader")
    with pytest.raises(InvitationMissingError):
        store.answer_invitation("group", 1, 2, group.group_id, member(2, "J"), accept=True)
    with pytest.raises(SocialContactBlockedError):
        store.issue_invitation("group", 1, 2, group.group_id, "t2")
    with pytest.raises(SocialContactBlockedError):
        store.issue_invitation("group-request", 2, 1, group.group_id, "t3")


def test_ignores_and_buddies_exclude_each_other(store):
    store.add_buddy(1, 2)
    with pytest.raises(SocialContactBlockedError):
        store.set_ignored(1, 2, "Friend")
    store.set_ignored(3, 1, "Owner")
    with pytest.raises(SocialContactBlockedError):
        store.add_buddy(1, 3)
    with pytest.raises(SocialContactBlockedError):
        store.add_buddy(3, 1)


def test_self_ignores_and_oversized_lists_are_refused(store):
    with pytest.raises(ValueError):
        store.set_ignored(1, 1, "Me")
    for target in range(2, IGNORE_LIST_LIMIT + 2):
        store.set_ignored(1, target, f"P{target}")
    with pytest.raises(ValueError):
        store.set_ignored(1, 10_000, "One too many")
