"""CMS hand-in of every open quest through the regular turn-in path."""

from dso_server.gameplay.quest_content import (
    QUEST_STATE_ACCEPTED, QUEST_STATE_CLOSED, QUEST_STATE_FINISHED, QuestProgress,
)
from dso_server.tests.test_admin_cms import cms, login, request
from dso_server.tests.test_admin_live import online

PATH = "/api/characters/42/complete-active-quests"
OPEN = "a0006-WayToKingshill-01-n"
FINISHED = "a0007-InfestedThreat-01-n"
CLOSED = "a0006-MissingChild-01-n"


def test_active_quests_are_handed_in_with_their_usual_rewards(cms, online):
    _, s = online
    s.gameplay_quest_log = {
        OPEN: QuestProgress(OPEN),
        FINISHED: QuestProgress(FINISHED, state=QUEST_STATE_FINISHED, finished_mask=1),
        CLOSED: QuestProgress(CLOSED, state=QUEST_STATE_CLOSED, completed_at="2026-01-01 00:00:00"),
    }
    gold, andermant = s.gameplay_currency_state.gold, s.gameplay_currency_state.andermant
    xp = s.gameplay_character.progression_u32
    cookie, csrf = login(cms)
    code, result, _ = request(cms, "POST", PATH, {"reason": "Skip quest content"}, cookie, csrf)
    assert code == 200, result
    assert result == {"ok": True, "completed": 2, "failed": []}
    log = s.gameplay_quest_log
    assert all(log[q].state == QUEST_STATE_CLOSED for q in (OPEN, FINISHED, CLOSED))
    assert log[CLOSED].completed_at == "2026-01-01 00:00:00"
    assert s.gameplay_currency_state.gold > gold
    assert s.gameplay_currency_state.andermant > andermant
    assert (s.gameplay_character.level, s.gameplay_character.progression_u32) != (1, xp)
    saved = cms.store.get(42).quest_log
    assert saved[OPEN]["state"] == saved[FINISHED]["state"] == QUEST_STATE_CLOSED
    assert any(row["action"] == "character.quests.complete_active"
               for row in cms.service.repository.history("admin_audit")["rows"])


def test_no_active_quests_or_offline_character_is_rejected(cms, online):
    _, s = online
    s.gameplay_quest_log = {
        CLOSED: QuestProgress(CLOSED, state=QUEST_STATE_CLOSED, completed_at="2026-01-01 00:00:00"),
    }
    cookie, csrf = login(cms)
    code, result, _ = request(cms, "POST", PATH, {"reason": "Skip quest content"}, cookie, csrf)
    assert code == 400 and "no active quests" in result["error"]
    s.gameplay_quest_log[OPEN] = QuestProgress(OPEN, state=QUEST_STATE_ACCEPTED)
    s.connected = False
    code, result, _ = request(cms, "POST", PATH, {"reason": "Skip quest content"}, cookie, csrf)
    assert code == 400 and "online" in result["error"]
    assert s.gameplay_quest_log[OPEN].state == QUEST_STATE_ACCEPTED
