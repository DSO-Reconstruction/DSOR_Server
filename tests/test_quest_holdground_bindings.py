"""Native defense request, XML bindings, and failed quest state."""
import struct
import xml.etree.ElementTree as ET

import pytest

from dso_server.gameplay.quest_content import (
    HoldGroundDefinition, QuestProgress, QUEST_STATE_FAILED, active_tasks,
    apply_quest_event, load_quest_catalog,
)
from dso_server.paths import DEFAULT_QUESTS_PATH
from dso_server.protocol.quest_protocol import DEFEND_COMMAND, Defend, decode_defend, encode_defend


def test_every_authored_defense_binding_and_failure_is_preserved():
    xml = ET.parse(DEFAULT_QUESTS_PATH).getroot()
    catalog = load_quest_catalog(DEFAULT_QUESTS_PATH)
    raw = xml.findall("Quest/Task/HoldGround")
    loaded = [binding for quest in catalog.quests.values() for task in quest.tasks for binding in task.holdgrounds]
    assert len(raw) == len(loaded) == 85
    assert {item.get("id") for item in raw} == {item.holdground_id for item in loaded}
    failures = [event for quest in catalog.quests.values() for task in quest.tasks
                for event in task.events if event.kind == "holdgroundfailed"]
    assert len(failures) == 85 and all(event.failure for event in failures)


def test_exact_difficulty_takes_precedence_over_authored_default():
    binding = HoldGroundDefinition("defend", "npc", ((None, "default"), (1, "hard")))
    assert binding.config_for_difficulty(0) == "default"
    assert binding.config_for_difficulty(1) == "hard"
    assert binding.config_for_difficulty(8) == "default"
    explicit = HoldGroundDefinition("defend", "npc", ((0, "normal"),))
    assert explicit.config_for_difficulty(1) == ""


def test_real_failure_is_state_four_and_cannot_complete_or_earn_credit():
    quest = load_quest_catalog(DEFAULT_QUESTS_PATH).quest("c2200-frogprince-02-n")
    progress = QuestProgress(quest.quest_id)
    binding = quest.tasks[0].holdgrounds[0]
    assert binding.live(progress.tokens)
    result = apply_quest_event(quest, progress, "holdgroundfailed", binding.holdground_id)
    assert result.quest_failed and not result.quest_finished
    assert progress.state == QUEST_STATE_FAILED
    assert not active_tasks(quest, progress)
    assert not progress.finished_mask and not progress.tokens
    assert QuestProgress.from_document(quest.quest_id, progress.to_document()).state == QUEST_STATE_FAILED
    result = apply_quest_event(quest, progress, "holdgroundsucceeded", binding.holdground_id)
    assert not result.changes and not result.quest_finished


def test_defend_matches_native_u32_actor_then_u16_length_string():
    value = Defend(0x12345678, "quest.s_defend")
    raw = value.holdground_id.encode("utf-8")
    packet = struct.pack("<BHIH", 0x8B, DEFEND_COMMAND, value.npc_actor_id, len(raw)) + raw
    assert decode_defend(packet) == value
    assert encode_defend(value) == (packet, len(packet) * 8)


@pytest.mark.parametrize("kind", ("truncated-actor", "truncated-string", "trailing"))
def test_defend_rejects_malformed_requests(kind):
    packet, _ = encode_defend(Defend(123, "quest.s_defend"))
    packet = packet[:5] if kind == "truncated-actor" else packet[:-1] if kind == "truncated-string" else packet + b"x"
    with pytest.raises((ValueError, EOFError, IndexError)):
        decode_defend(packet)
