"""Talent rules the client enforces and the server now does too: Honor
deselection, bought Skill presets, the membership's preset count, preset
cooldowns outside hubs, gold-priced resets, Knowledge/Group unlock levels,
skill-talent groups and the Wisdom trader's trait."""

from dataclasses import replace
import time
import unittest
from unittest.mock import Mock, patch

from dso_server.persistence.account_state import (
    _state_from_document,
    _state_to_document,
)
from dso_server.protocol.gameplay_protocol import STATUS_EFFECT_COMMAND
from dso_server.protocol.player_init_protocol import (
    CURRENCY_CHANGED_COMMAND,
    TALENT_SELECTION_INFO_COMMAND,
    UNLOCK_SKILL_TALENT_PRESELECTION_INFO_COMMAND,
    decode_player_init_prefix,
)
from dso_server.protocol.shop_protocol import RequestOffer
from dso_server.protocol.talent_protocol import (
    DeselectTalent,
    ResetTalentCategory,
    ResetTalentPreselection,
    SelectTalent,
    SelectTalentPreselection,
    TalentDatabase,
    UnlockSkillTalentPreselection,
    decode_deselect_talent,
    decode_unlock_skill_talent_preselection,
    encode_deselect_talent,
    encode_reset_talent_category,
    encode_reset_talent_preselection,
    encode_select_talent,
    encode_select_talent_preselection,
    encode_unlock_skill_talent_preselection,
    talent_preselection_available,
    talent_respec_cost,
)
from dso_server.runtime.transport import Session
from dso_server.services import talents as talent_service
from dso_server.services.base import CharacterService
from dso_server.tests.fixtures.characters import character_server

HUB = "a0200_kingscity"
DUNGEON = "a0303_wildcave"
FOCUS = "fame_focus_talent_01_honorbonus"
TRAIT = "mage_fame_trait_04_supportaura"
WISDOM = "trait_wisdom_all_damage"  # UnlockLevel 30, 3 points, branch attack
MASTERY = "mage_talent_fire_master"  # GroupId mageTalentElemental, 5 points
RIVAL = "mage_talent_poison_master"
LEVEL_100 = 207448.0  # _Template_DynamicLevelCosts.RespecVCCostMultiplier


class TalentProtocolTests(unittest.TestCase):
    def test_new_commands_round_trip(self):
        self.assertEqual(
            decode_deselect_talent(*encode_deselect_talent(
                DeselectTalent(FOCUS)
            )),
            DeselectTalent(FOCUS),
        )
        payload, bits = encode_unlock_skill_talent_preselection(
            UnlockSkillTalentPreselection(1)
        )
        self.assertEqual(payload.hex(), "8b1c0101000000")
        self.assertEqual(
            decode_unlock_skill_talent_preselection(payload, bits).unlocked, 1
        )
        with self.assertRaises(ValueError):
            encode_unlock_skill_talent_preselection(
                UnlockSkillTalentPreselection(5)
            )

    def test_preset_rules_follow_the_client(self):
        def available(category, preset, sets, bought=0):
            return talent_preselection_available(
                category, preset, talent_sets=sets,
                unlocked_skill_presets=bought,
            )

        self.assertTrue(available(0, 1, 2))
        self.assertFalse(available(0, 2, 2))
        self.assertTrue(available(1, 3, 4))
        self.assertFalse(available(2, 3, 3))
        # Bought Skill presets start at index 4, whatever the membership.
        self.assertFalse(available(0, 4, 2))
        self.assertTrue(available(0, 4, 2, bought=1))
        self.assertFalse(available(0, 5, 4, bought=1))
        self.assertFalse(available(1, 4, 4, bought=4))
        self.assertTrue(available(3, 0, 2))

    def test_respec_price_formula(self):
        self.assertEqual(
            talent_respec_cost(
                spent_points=3, cost_per_point=1, level_multiplier=400.0
            ),
            1200,
        )
        # Above 999999 the price drops to a multiple of 10000.
        self.assertEqual(
            talent_respec_cost(
                spent_points=10, cost_per_point=1,
                level_multiplier=LEVEL_100,
            ),
            2070000,
        )
        self.assertEqual(
            talent_respec_cost(
                spent_points=3, cost_per_point=1, level_multiplier=400.0,
                factor=0.5,
            ),
            600,
        )

    def test_niwalk_traits_have_a_class(self):
        self.assertEqual(
            TalentDatabase._class_from_talent_id("niwalk_fame_trait_x"),
            "niwalk",
        )
        self.assertEqual(
            TalentDatabase._class_from_talent_id("Dwarf_fame_trait_x"),
            "dwarf",
        )


class TalentServiceTests(unittest.TestCase):
    def setUp(self):
        self.server = character_server(self)
        if self.server.talent_database is None:
            self.skipTest("the static database is not installed")
        self.server.sock = Mock()
        self.sent = []
        real = talent_service.encode_server_command_batch

        def record(commands, *args, **kwargs):
            self.sent.extend(commands)
            return real(commands, *args, **kwargs)

        recorder = patch.object(
            talent_service, "encode_server_command_batch", side_effect=record
        )
        recorder.start()
        self.addCleanup(recorder.stop)

    def enter(self, map_id=HUB):
        s = Session(("127.0.0.1", 51003), self.server.sock)
        s.connected = s.gameplay_mode = True
        s.gameplay_map = map_id
        s.gameplay_character = self.server.character_selection.active_character
        s.send_reliable_ordered_split = Mock()
        s.send_reliable_ordered = Mock()
        self.addCleanup(self.cancel_timers, s)
        self.server._send_local_player_initialization(s)
        call = next(
            c for c in s.send_reliable_ordered_split.call_args_list
            if c.kwargs.get("label_prefix")
            == "local-player-initialization-split"
        )
        batch, _ = decode_player_init_prefix(
            call.args[0], call.kwargs["total_bit_length"]
        )
        self.login_sent = list(self.sent)
        self.sent.clear()
        return s, batch

    @staticmethod
    def cancel_timers(s):
        for timer in tuple(s.gameplay_projectile_timers.values()):
            timer.cancel()

    def wallet(self, s, *, gold=None, andermant=None):
        currency = s.gameplay_currency_state
        s.gameplay_currency_state = replace(
            currency,
            amount=currency.amount if gold is None else gold,
            currency_type=(
                currency.andermant if andermant is None else andermant
            ),
        )

    def select(self, s, talent_id, ranks=1):
        self.server._handle_select_talent(
            s, encode_select_talent(SelectTalent(talent_id, ranks))[0]
        )

    def preset(self, s, category, preset):
        self.server._handle_select_talent_preselection(
            s,
            encode_select_talent_preselection(
                SelectTalentPreselection(preset, category)
            )[0],
        )

    def rank(self, s, category, talent_id):
        return next(
            (
                int(entry.field_00)
                for entry in s.gameplay_talent_categories[category]
                .selected_skills
                if entry.resource_id == talent_id
            ),
            0,
        )

    def sent_values(self, command_id):
        return [c.value for c in self.sent if c.command_id == command_id]

    def cooldowns(self, s, wire_id):
        return [
            effect for effect in s.gameplay_persistent_status_effects.values()
            if int(effect.effect_id) == wire_id
        ]

    def test_deselect_takes_a_focus_rank_and_switches_a_trait_off(self):
        s, _ = self.enter()
        self.select(s, FOCUS, 3)
        self.select(s, TRAIT)
        self.assertEqual(self.rank(s, 3, FOCUS), 3)
        self.assertEqual(self.rank(s, 3, TRAIT), 1)
        trait_effects = s.gameplay_talent_effect_instance_ids[f"3:{TRAIT}"]
        self.sent.clear()

        self.server._handle_deselect_talent(
            s, encode_deselect_talent(DeselectTalent(FOCUS))[0]
        )
        self.assertEqual(self.rank(s, 3, FOCUS), 2)
        self.server._handle_deselect_talent(
            s, encode_deselect_talent(DeselectTalent(TRAIT))[0]
        )
        self.assertEqual(self.rank(s, 3, TRAIT), 0)
        self.assertNotIn(f"3:{TRAIT}", s.gameplay_talent_effect_instance_ids)
        for instance_id in trait_effects:
            self.assertNotIn(instance_id, s.gameplay_persistent_status_effects)
        self.assertEqual(
            s.gameplay_category_talent_presets[3][0],
            s.gameplay_talent_categories[3].selected_skills,
        )
        self.assertTrue(any(
            value.selection_type == 0
            for value in self.sent_values(TALENT_SELECTION_INFO_COMMAND)
        ))
        self.assertTrue(any(
            not entry.has_details
            for value in self.sent_values(STATUS_EFFECT_COMMAND)
            for entry in value.entries
        ))

        # The last focus rank goes too; a talent not chosen is refused, and
        # so is any talent outside the Honor tree.
        for _ in range(2):
            self.server._handle_deselect_talent(
                s, encode_deselect_talent(DeselectTalent(FOCUS))[0]
            )
        self.assertEqual(self.rank(s, 3, FOCUS), 0)
        before = s.gameplay_talent_categories
        self.server._handle_deselect_talent(
            s, encode_deselect_talent(DeselectTalent(FOCUS))[0]
        )
        self.select(s, MASTERY)
        self.server._handle_deselect_talent(
            s, encode_deselect_talent(DeselectTalent(MASTERY))[0]
        )
        self.assertEqual(self.rank(s, 3, FOCUS), 0)
        self.assertEqual(self.rank(s, 0, MASTERY), 1)
        self.assertEqual(before[3], s.gameplay_talent_categories[3])

    def test_knowledge_unlock_level_is_the_character_level(self):
        s, _ = self.enter()
        # Knowledge level 0 grants 9 points; the talent unlocks at level 30.
        self.select(s, WISDOM)
        self.assertEqual(self.rank(s, 1, WISDOM), 1)
        s.gameplay_character = replace(s.gameplay_character, level=20)
        self.select(s, WISDOM)
        self.assertEqual(self.rank(s, 1, WISDOM), 1)

    def test_one_skill_talent_per_group(self):
        s, _ = self.enter()
        self.select(s, MASTERY)
        self.select(s, RIVAL)
        self.assertEqual(self.rank(s, 0, MASTERY), 1)
        self.assertEqual(self.rank(s, 0, RIVAL), 0)
        # Raising the chosen one is fine.
        self.select(s, MASTERY)
        self.assertEqual(self.rank(s, 0, MASTERY), 2)

    def test_buying_skill_presets_charges_andermant_and_survives_a_relog(self):
        s, batch = self.enter()
        self.assertEqual(
            next(
                c.value.unlocked_count for c in batch
                if c.command_id
                == UNLOCK_SKILL_TALENT_PRESELECTION_INFO_COMMAND
            ),
            0,
        )
        self.wallet(s, andermant=15000)

        def unlock(count):
            self.server._handle_unlock_skill_talent_preselection(
                s,
                encode_unlock_skill_talent_preselection(
                    UnlockSkillTalentPreselection(count)
                )[0],
            )

        unlock(2)  # not the next one
        self.assertEqual(s.gameplay_unlocked_skill_presets, 0)
        self.assertEqual(
            self.sent_values(UNLOCK_SKILL_TALENT_PRESELECTION_INFO_COMMAND)[-1]
            .unlocked_count,
            0,
        )
        unlock(1)
        self.assertEqual(s.gameplay_unlocked_skill_presets, 1)
        self.assertEqual(s.gameplay_currency_state.andermant, 5000)
        self.assertEqual(
            self.sent_values(CURRENCY_CHANGED_COMMAND)[-1].andermant, 5000
        )
        self.assertEqual(
            self.sent_values(UNLOCK_SKILL_TALENT_PRESELECTION_INFO_COMMAND)[-1]
            .unlocked_count,
            1,
        )
        unlock(2)  # 30000 Andermant, 5000 left
        self.assertEqual(s.gameplay_unlocked_skill_presets, 1)
        self.assertEqual(s.gameplay_currency_state.andermant, 5000)

        # The bought preset can be switched to.
        self.preset(s, 0, 4)
        self.assertEqual(s.gameplay_talent_categories[0].preselection, 4)

        relogged, batch = self.enter()
        self.assertEqual(relogged.gameplay_unlocked_skill_presets, 1)
        self.assertEqual(
            next(
                c.value.unlocked_count for c in batch
                if c.command_id
                == UNLOCK_SKILL_TALENT_PRESELECTION_INFO_COMMAND
            ),
            1,
        )
        saved = self.server.account_store.get(
            relogged.gameplay_character.character_id
        )
        self.assertEqual(
            _state_from_document(_state_to_document(saved))
            .unlocked_skill_presets,
            1,
        )

    def test_presets_follow_the_membership(self):
        s, _ = self.enter()
        s.gameplay_membership_active = False
        self.preset(s, 1, 2)
        self.assertEqual(s.gameplay_talent_categories[1].preselection, 0)
        self.preset(s, 1, 1)
        self.assertEqual(s.gameplay_talent_categories[1].preselection, 1)
        s.gameplay_membership_active = True  # Deluxe: four presets
        self.preset(s, 1, 3)
        self.assertEqual(s.gameplay_talent_categories[1].preselection, 3)
        self.preset(s, 0, 4)  # bought presets only
        self.assertEqual(s.gameplay_talent_categories[0].preselection, 0)

    def test_switching_outside_a_hub_starts_a_cooldown(self):
        s, _ = self.enter()
        _, wire_id, seconds = self.server.talent_database.preset_cooldown(1)
        # Hubs switch freely.
        self.preset(s, 1, 1)
        self.preset(s, 1, 0)
        self.assertEqual(s.gameplay_talent_categories[1].preselection, 0)
        self.assertFalse(self.cooldowns(s, wire_id))

        s.gameplay_map = DUNGEON
        self.preset(s, 1, 1)
        self.assertEqual(s.gameplay_talent_categories[1].preselection, 1)
        (cooldown,) = self.cooldowns(s, wire_id)
        self.assertEqual(cooldown.field_10, int(seconds * 25))
        self.assertAlmostEqual(
            s.gameplay_timed_status_effect_expiries[cooldown.field_04],
            time.time() + seconds,
            delta=5,
        )
        key = (talent_service.TALENT_PRESET_COOLDOWN_TIMER, cooldown.field_04)
        self.assertIn(key, s.gameplay_projectile_timers)
        self.assertIn(
            cooldown,
            [
                entry
                for value in self.sent_values(STATUS_EFFECT_COMMAND)
                for entry in value.entries
            ],
        )

        # Refused while it runs; other trees have their own cooldown, and
        # re-selecting the active preset changes nothing.
        self.preset(s, 1, 2)
        self.assertEqual(s.gameplay_talent_categories[1].preselection, 1)
        self.preset(s, 1, 1)
        self.assertEqual(len(self.cooldowns(s, wire_id)), 1)
        self.preset(s, 2, 1)
        self.assertEqual(s.gameplay_talent_categories[2].preselection, 1)

        # At expiry the client is told to drop the effect.
        self.sent.clear()
        s.gameplay_projectile_timers.pop(key).cancel()
        with patch.object(CharacterService, "_request_account_save"):
            CharacterService._expire_talent_preset_cooldown(
                s, s.gameplay_chase_generation, key, cooldown
            )
        self.assertFalse(self.cooldowns(s, wire_id))
        self.assertNotIn(
            cooldown.field_04, s.gameplay_timed_status_effect_expiries
        )
        (removal,) = [
            entry
            for value in self.sent_values(STATUS_EFFECT_COMMAND)
            for entry in value.entries
        ]
        self.assertEqual(removal.field_04, cooldown.field_04)
        self.assertFalse(removal.has_details)
        self.preset(s, 1, 2)
        self.assertEqual(s.gameplay_talent_categories[1].preselection, 2)

    def test_cooldowns_rearm_after_a_map_load_and_lift_in_a_hub(self):
        s, _ = self.enter(DUNGEON)
        _, wire_id, _ = self.server.talent_database.preset_cooldown(0)
        self.preset(s, 0, 1)
        (cooldown,) = self.cooldowns(s, wire_id)

        again, _ = self.enter(DUNGEON)
        (restored,) = self.cooldowns(again, wire_id)
        self.assertEqual(restored.field_04, cooldown.field_04)
        self.assertIn(
            (talent_service.TALENT_PRESET_COOLDOWN_TIMER, cooldown.field_04),
            again.gameplay_projectile_timers,
        )

        town, _ = self.enter(HUB)
        self.assertFalse(self.cooldowns(town, wire_id))
        self.assertNotIn(
            cooldown.field_04, town.gameplay_timed_status_effect_expiries
        )
        self.assertTrue(any(
            entry.field_04 == cooldown.field_04 and not entry.has_details
            for command in self.login_sent
            if command.command_id == STATUS_EFFECT_COMMAND
            for entry in command.value.entries
        ))

    def test_resets_cost_gold_and_honor_is_free(self):
        s, _ = self.enter()
        self.select(s, MASTERY)  # 5 points
        price = talent_respec_cost(
            spent_points=5, cost_per_point=1, level_multiplier=LEVEL_100
        )
        self.assertEqual(price, 1030000)

        def reset(category):
            self.server._handle_reset_talent_preselection(
                s,
                encode_reset_talent_preselection(ResetTalentPreselection(
                    category, s.gameplay_talent_categories[category]
                    .preselection,
                ))[0],
            )

        self.wallet(s, gold=price - 1)
        reset(0)
        self.assertEqual(self.rank(s, 0, MASTERY), 1)
        self.assertEqual(s.gameplay_currency_state.gold, price - 1)

        self.wallet(s, gold=price + 7)
        reset(0)
        self.assertEqual(self.rank(s, 0, MASTERY), 0)
        self.assertEqual(s.gameplay_currency_state.gold, 7)
        self.assertEqual(self.sent_values(CURRENCY_CHANGED_COMMAND)[-1].gold, 7)

        self.select(s, FOCUS, 2)
        reset(3)
        self.assertEqual(self.rank(s, 3, FOCUS), 0)
        self.assertEqual(s.gameplay_currency_state.gold, 7)

    def test_branch_reset_costs_gold(self):
        s, _ = self.enter()
        self.select(s, WISDOM)  # 3 points in trait_wisdom_attack
        self.wallet(s, gold=1000000)
        self.server._handle_reset_talent_category(
            s,
            encode_reset_talent_category(ResetTalentCategory(
                1, "trait_wisdom_attack",
                s.gameplay_talent_categories[1].preselection,
            ))[0],
        )
        self.assertEqual(self.rank(s, 1, WISDOM), 0)
        self.assertEqual(
            s.gameplay_currency_state.gold, 1000000 - round(3 * LEVEL_100)
        )

    def test_the_wisdom_trader_needs_its_trait(self):
        s, _ = self.enter()
        request = RequestOffer(True, 0, "skilltree_wisdom_trader")
        with patch(
            "dso_server.services.commerce.decode_request_offer",
            return_value=request,
        ), patch.object(
            type(self.server), "_build_shop_offer_snapshot",
            side_effect=RuntimeError("served"),
        ):
            self.server._handle_request_offer(s, b"")
            self.assertIsNone(s.gameplay_open_shop_id)
            knowledge = s.gameplay_talent_categories[1]
            s.gameplay_talent_categories = (
                s.gameplay_talent_categories[0],
                replace(knowledge, selected_skills=(
                    replace(
                        knowledge.selected_skills[0],
                        resource_id="trait_wisdom_all_dungeon_shop",
                    )
                    if knowledge.selected_skills
                    else talent_service.TalentSelectionEntry(
                        1, 0, "trait_wisdom_all_dungeon_shop"
                    ),
                )),
                *s.gameplay_talent_categories[2:],
            )
            with self.assertRaisesRegex(RuntimeError, "served"):
                self.server._handle_request_offer(s, b"")


if __name__ == "__main__":
    unittest.main()
