"""Quest markers must be present in the NPCs published on map entry."""

import struct

import pytest

from dso_server.gameplay.local_map_state import load_local_map_initialization
from dso_server.gameplay.quest_content import QUEST_STATE_CLOSED, QuestProgress
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import (
    NEW_NPC_COMMAND, NPC_INFO_COMMAND, NPC_REQUEST_COMMAND, decode_npc_batch,
)
from dso_server.protocol.player_init_protocol import (
    decode_server_command_batch, encode_server_command_batch,
)
from dso_server.tests import test_quests as fixtures


@pytest.fixture
def quest_case():
    case = fixtures.QuestServiceTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def initial_npc_info(case, session, template):
    commands, _exits = load_local_map_initialization(
        DEFAULT_STATIC_DB_PATH, session.gameplay_map,
    )
    commands = case.server._quest_decorate_map_commands(session, commands)
    commands = decode_server_command_batch(*encode_server_command_batch(commands))
    actor = next(c.value.actor_id for c in commands
                 if c.command_id == NEW_NPC_COMMAND and c.value.template_id == template)
    return next(c.value for c in commands
                if c.command_id == NPC_INFO_COMMAND and c.value.actor_id == actor)


@pytest.mark.parametrize("map_id,prefix", [
    ("a0200_kingscity", "a0200"),
    ("b1100_hub", "b1100"),
])
@pytest.mark.parametrize("level,completed_intro", [(14, False), (15, False), (100, False), (100, True)])
def test_pvp_offers_on_entry_match_npc_dialog(quest_case, map_id, prefix, level, completed_intro):
    case = quest_case
    session = case._session(map_id, 50109, class_id=2)
    case.server.sessions[session.addr] = session
    session.gameplay_character.level = level
    case.server._attach_map_event_runtime(
        session, map_id=map_id, initial_flags=frozenset(),
        monster_level=None, scaling_difficulty=0, fresh_instance=True,
    )
    first = f"pvp-{prefix}-participation-01-n"
    repeatable = f"pvp-{prefix}-participation-02-n"
    if completed_intro:
        session.gameplay_quest_log[first] = QuestProgress(first, state=QUEST_STATE_CLOSED)

    # Read the initial NewNPC/NPCInfo wire batch before sending any request.
    initial = initial_npc_info(case, session, f"{prefix}_merchant_arena")
    # Arena merchants share the one generation-1 NPC sequence (gameplay/
    # local_map_state.py local_npc_actor_id): a generation-2 id moved their
    # client slot onto the props'.
    assert initial.actor_id < 0x20000
    offers = {i.source: i for i in initial.interactions}
    if level < 15:
        assert not offers
    elif completed_intro:
        assert first not in offers
        assert offers[repeatable].field_18 == 3
    else:
        assert offers[first].field_18 == 9
        assert repeatable not in offers

    # Opening the dialog must not be needed to populate the initial markers.
    case.server._handle_npc_request(
        session, b"\x8b" + struct.pack("<HI", NPC_REQUEST_COMMAND, initial.actor_id),
    )
    replies = [decode_npc_batch(data, bits).commands[0]
               for data, bits, label in session.recorder.messages
               if label == "npc-info-response"]
    assert replies == [initial]


def test_ordinary_quest_giver_keeps_initial_marker(quest_case):
    initial = initial_npc_info(quest_case, quest_case.session, "a0001_start_starter")
    assert initial.actor_id < 0x20000
    assert [(i.source, i.field_08, i.field_18) for i in initial.interactions] == [
        (fixtures.TUTORIAL_01, fixtures.NPC_QUEST_DIALOG, 6),
    ]
