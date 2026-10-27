"""Current-client quest additions and compatibility with pre-261_1024 saves."""

import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dso_server.gameplay.quest_content import (
    NO_TIMESTAMP,
    QUEST_STATE_ACCEPTED,
    QUEST_STATE_CANCELLED,
    QUEST_STATE_CLOSED,
    QUEST_STATE_FINISHED,
    QuestProgress,
    active_tasks,
    apply_quest_event,
    load_quest_catalog,
)
from dso_server.paths import DEFAULT_QUESTS_PATH
from dso_server.services.quests import QuestService


SUMMER_QUESTS = (
    "e-sum-quest-2024",
    "e-sum-wolf-quest-new-n",
    "e-sum-wolf-quest-new-n-jewel",
)
FRACTURED_QUESTS = (
    "fracturedplanes-main-01-01",
    *(f"fracturedplanes-main-02-0{n}" for n in range(1, 5)),
    *(f"fracturedplanes-main-03-0{n}" for n in range(1, 5)),
    "fracturedplanes-main-04-01",
)


@pytest.fixture
def catalog():
    return load_quest_catalog(DEFAULT_QUESTS_PATH)


@pytest.fixture
def service(catalog):
    service = QuestService()
    service._quest_catalog = lambda: catalog
    service._quest_season = lambda: (frozenset(), frozenset())
    service._quest_grant_rewards = Mock()
    return service


def restore(quest_id, document, catalog):
    session = SimpleNamespace()
    QuestService._restore_quest_state(
        session, SimpleNamespace(quest_log={quest_id: document}), catalog=catalog,
    )
    return session, session.gameplay_quest_log[quest_id]


def legacy_summer_document(quest_id, *, state=QUEST_STATE_ACCEPTED, delivered=True):
    """Old saves had a third, final NPC encounter at bit 2."""
    prefix = "e-sum-quest-2024" if quest_id == "e-sum-quest-2024" else "e-sum-wolf-quest"
    delivery = "delivery" if quest_id == "e-sum-quest-2024" else "deliver"
    tokens = {f"{prefix}-talkDialogTalked": 1, f"{prefix}-talk_fin": 1}
    mask = 1
    if delivered:
        tokens.update({
            f"{prefix}-{delivery}DeliveredLoot": 100 if quest_id.endswith("-jewel") else 30,
            f"{prefix}-{delivery}_fin": 1,
        })
        mask = 3
    if state in (QUEST_STATE_FINISHED, QUEST_STATE_CLOSED):
        tokens.update({f"{prefix}-completeInteract": 1, f"{prefix}-complete_fin": 1})
        mask = 7
    return {
        "state": state, "tokens": tokens, "finished_mask": mask,
        "completed_at": "2026-10-20 12:34:56" if state == QUEST_STATE_CLOSED else NO_TIMESTAMP,
        "map_instance": "", "continue_on_map": "",
    }


def test_fractured_planes_chain_is_available_only_after_its_prerequisites(catalog):
    for quest_id in FRACTURED_QUESTS:
        quest = catalog.quest(quest_id)
        assert quest is not None
        assert quest in catalog.offered_by(quest.giver)
        assert quest in catalog.completed_at(quest.goal)
        facts = dict(
            class_name="ranger", level=100, known_quests=(),
            closed_quests=quest.required,
            active_events=("event_fracturedplanes",),
        )
        assert catalog.available(quest, **facts)
        assert not catalog.available(quest, **{**facts, "level": 99})
        if quest.required:
            assert not catalog.available(quest, **{**facts, "closed_quests": ()})
        if quest.event_id:
            assert not catalog.available(quest, **{**facts, "active_events": ()})
        for following_id in quest.followups:
            assert quest.quest_id in catalog.quest(following_id).required


def test_fractured_planes_introduction_and_followup_progress(catalog):
    quest = catalog.quest(FRACTURED_QUESTS[0])
    progress = QuestProgress(quest.quest_id)
    # Kills before exploring do not skip the authored first stage.
    assert not apply_quest_event(quest, progress, "kill", "all_guardian_merciless_ripper_04", count=5).changes
    apply_quest_event(quest, progress, "trigger", "fractured0101Trigger")
    apply_quest_event(quest, progress, "kill", "all_guardian_merciless_ripper_04", count=5)
    assert progress.finished_mask == 3
    assert apply_quest_event(quest, progress, "encounter", "npc_aurelion_catacombs").quest_finished

    followup = catalog.quest(quest.followups[0])
    progress = QuestProgress(followup.quest_id)
    result = apply_quest_event(followup, progress, "kill", "g0001_normal_meatmummy_melee_02", count=15)
    assert any(drop.item_id == "e_fp_questitem_rift_fragment" for drop in result.drops)
    assert progress.finished_mask == 1  # Ground loot still requires pickup.
    apply_quest_event(followup, progress, "collect", "e_fp_questitem_rift_fragment", count=15)
    assert progress.finished_mask == 3
    assert apply_quest_event(followup, progress, "encounter", "npc_aurelion_kingshill").quest_finished


@pytest.mark.parametrize("quest_id", SUMMER_QUESTS)
def test_delivered_legacy_summer_save_is_ready_without_removed_encounter(catalog, service, quest_id):
    document = legacy_summer_document(quest_id)
    original = copy.deepcopy(document)
    session, progress = restore(quest_id, document, catalog)
    assert len(catalog.quest(quest_id).tasks) == 2
    assert progress.state == QUEST_STATE_FINISHED
    assert progress.finished_mask == 3
    assert progress.tokens == original["tokens"]
    assert progress.completed_at == NO_TIMESTAMP  # Turn-in, not restore, pays the reward.
    entry, = service._quest_log_info(session).quests
    assert entry.state == QUEST_STATE_FINISHED
    assert entry.field_10 == 3
    assert entry.progress[1] == (100 if quest_id.endswith("-jewel") else 30)
    service._quest_grant_rewards.assert_not_called()
    assert document == original
    saved = service._quest_log_document(session)
    restored, _ = restore(quest_id, saved[quest_id], catalog)
    assert service._quest_log_document(restored) == saved


@pytest.mark.parametrize("quest_id", SUMMER_QUESTS)
def test_partial_legacy_summer_save_retains_counters_and_can_finish(catalog, quest_id):
    document = legacy_summer_document(quest_id, delivered=False)
    session, progress = restore(quest_id, document, catalog)
    quest = catalog.quest(quest_id)
    assert progress.state == QUEST_STATE_ACCEPTED
    assert progress.tokens == document["tokens"]
    assert [task.index for task in active_tasks(quest, progress)] == [1]
    delivery = quest.tasks[1].barters[0]
    assert apply_quest_event(quest, progress, "deliver", delivery.barter_id).quest_finished
    assert progress.finished_mask == 3


@pytest.mark.parametrize("quest_id", SUMMER_QUESTS)
@pytest.mark.parametrize("state", (QUEST_STATE_FINISHED, QUEST_STATE_CLOSED, QUEST_STATE_CANCELLED))
def test_restore_preserves_finished_closed_and_cancelled_summer_states(catalog, service, quest_id, state):
    document = legacy_summer_document(quest_id, state=state)
    session, progress = restore(quest_id, document, catalog)
    assert progress.state == state
    assert progress.finished_mask == 3
    assert progress.tokens == document["tokens"]
    assert progress.completed_at == document["completed_at"]
    if state == QUEST_STATE_CLOSED:
        assert not service._quest_turn_in(session, quest_id)
    service._quest_grant_rewards.assert_not_called()


@pytest.mark.parametrize("quest_id,item_id", (
    ("e-sum-effect-quest-n", "vanity_groundmarker_40"),
    ("e-sum-wolf-quest-new-n", "costume_effect_summer2026"),
))
def test_summer_rewards_match_current_client_for_every_class(catalog, quest_id, item_id):
    quest = catalog.quest(quest_id)
    for class_name in ("warrior", "mage", "ranger", "dwarf", "niwalk"):
        assert [reward.value for reward in quest.rewards_for_class(class_name) if reward.kind == "loot"] == [item_id]


def test_unknown_quest_history_is_not_deleted_by_content_sync(catalog):
    document = {
        "state": QUEST_STATE_CLOSED, "tokens": {"legacy": 3},
        "finished_mask": 7, "completed_at": "2026-10-20 12:34:56",
        "map_instance": "old-instance", "continue_on_map": "old-map",
    }
    _, progress = restore("unknown-legacy-quest", document, catalog)
    assert progress.to_document() == document


def test_fractured_introduction_service_offers_accepts_and_pays_turn_in_once():
    from dso_server.protocol.player_init_protocol import CurrencyChanged
    from dso_server.protocol.quest_protocol import (
        QUEST_OPERATION_ACCEPT, QUEST_OPERATION_TURN_IN,
        QuestCommand, encode_quest_command,
    )
    from dso_server.tests.test_quests import QuestServiceTests

    case = QuestServiceTests(methodName="runTest")
    case.setUp()
    try:
        server = case.server
        session = case._session("a0200_kingscity", 50193, class_id=2)
        session.gameplay_character.level = 100
        session.gameplay_currency_state = CurrencyChanged(0, 0, (0, 0, 0), 0, False)
        server.sessions[session.addr] = session
        server._attach_map_event_runtime(
            session, map_id=session.gameplay_map, initial_flags=frozenset(),
            monster_level=100, scaling_difficulty=0, fresh_instance=True,
        )
        quest_id = FRACTURED_QUESTS[0]
        interactions, _ = server._quest_npc_interactions(session, "a0200_merchant_priest")
        assert quest_id in {item.source for item in interactions}

        def command(operation):
            payload, bits = encode_quest_command(QuestCommand(quest_id, operation))
            session.current_message_bit_length = bits
            server._handle_quest_command(session, payload)

        command(QUEST_OPERATION_ACCEPT)
        assert session.gameplay_quest_log[quest_id].state == QUEST_STATE_ACCEPTED
        server._quest_leave_map(session, "a0202_kingscrypt01")
        session.gameplay_map = session.gameplay_map_access_group_id = "a0202_kingscrypt01"
        server._attach_map_event_runtime(
            session, map_id=session.gameplay_map, initial_flags=frozenset(),
            monster_level=100, scaling_difficulty=0, fresh_instance=True,
        )
        server._quest_feed(session, "trigger", "fractured0101Trigger", quest_id=quest_id)
        server._quest_feed(session, "kill", "all_guardian_merciless_ripper_04", count=5, quest_id=quest_id)
        server._quest_on_npc_encounter(session, "npc_aurelion_catacombs")
        assert session.gameplay_quest_log[quest_id].state == QUEST_STATE_FINISHED
        command(QUEST_OPERATION_TURN_IN)
        assert session.gameplay_quest_log[quest_id].state == QUEST_STATE_CLOSED
        assert session.gameplay_currency_state.andermant == 90
        assert session.gameplay_currency_state.gold == 345
        interactions, _ = server._quest_npc_interactions(session, "npc_aurelion_catacombs")
        assert "fracturedplanes-main-02-01" in {item.source for item in interactions}

        saved = server._quest_log_document(session)
        server._restore_quest_state(session, SimpleNamespace(quest_log=saved))
        command(QUEST_OPERATION_TURN_IN)
        assert server._quest_log_document(session) == saved
        assert session.gameplay_currency_state.andermant == 90
        assert session.gameplay_currency_state.gold == 345
    finally:
        case.doCleanups()
