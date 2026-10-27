from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import unittest

from dso_server.paths import (
    DEFAULT_STATIC_DB_PATH,
)
from dso_server.gameplay.status_modifier_runtime import (
    ModifierAmounts,
    active_xp_gain_multiplier,
    apply_status_drop_amounts,
    evaluate_active_status_modifiers,
    instant_current_xp_gain,
    load_status_modifier_catalog,
    parse_modifier,
    resolve_modifier_value,
    snapshot_active_status_entries,
)
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog


@dataclass(frozen=True)
class _Item:
    template_id: str
    amount: int


@dataclass(frozen=True)
class _Drop:
    item: _Item


@dataclass(frozen=True)
class _Generation:
    drops: tuple[_Drop, ...]


def _entry(effect_id: int, *, stack_index: int = 0, values=()):
    return SimpleNamespace(
        effect_id=effect_id,
        field_18=stack_index,
        values=tuple(values),
    )


class StatusModifierRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_status_modifier_catalog(DEFAULT_STATIC_DB_PATH)
        cls.effect_ids = load_status_runtime_catalog(DEFAULT_STATIC_DB_PATH).effect_ids

    def test_parser_preserves_arguments_and_strips_debug_label(self):
        modifier = parse_modifier(
            'DropAmount:$0,relative,money_gc#wisdom_bonus'
        )
        self.assertEqual(modifier.kind, 'dropamount')
        self.assertEqual(
            modifier.arguments, ('$0', 'relative', 'money_gc')
        )
        self.assertEqual(modifier.label, 'wisdom_bonus')

    def test_value_resolution_supports_parameters_and_actor_variables(self):
        self.assertEqual(resolve_modifier_value('$1', (2.0, 3.5)), 3.5)
        self.assertEqual(
            resolve_modifier_value(
                '$Some_Variable', (), {'some_variable': 0.25}
            ),
            0.25,
        )
        self.assertIsNone(resolve_modifier_value('$8', (1.0,)))

    def test_active_instant_buff_exposes_all_three_bonus_families(self):
        modifiers = evaluate_active_status_modifiers(
            self.catalog,
            (_entry(self.effect_ids['buff_reactivation_campaign_instant_01']),),
        )
        self.assertEqual(modifiers.xp_gain, ModifierAmounts(relative=1.0))
        self.assertEqual(
            modifiers.virtual_currency_drop,
            ModifierAmounts(relative=1.0),
        )
        self.assertEqual(
            modifiers.drop_amount_bonus('money_gc'),
            ModifierAmounts(relative=1.0),
        )
        self.assertEqual(modifiers.xp_gain_multiplier, 2.0)

    def test_xp_gain_stack_count_is_clamped_to_static_maximum(self):
        # e_qaizah_start_xpgain is +2.5% and has MaxStackSize 10.  A corrupt
        # or stale wire stack index must never amplify it beyond +25%.
        effect = _entry(self.effect_ids['e_qaizah_start_xpgain'], stack_index=99)
        self.assertAlmostEqual(
            active_xp_gain_multiplier(DEFAULT_STATIC_DB_PATH, (effect,)),
            1.25,
        )

    def test_curr_xp_is_resolved_as_a_one_shot_gain(self):
        definition = SimpleNamespace(
            start_modifiers='',
            tick_modifiers='CurrXP:$0,absolute',
            values=(7.0,),
            max_stack_size=1,
        )
        self.assertEqual(
            instant_current_xp_gain(
                definition, current_xp=100, stack_count=99
            ),
            7,
        )

    def test_drop_application_covers_gold_wisdom_and_exclusions(self):
        generation = _Generation((
            _Drop(_Item('money_vc', 10)),
            _Drop(_Item('money_gc', 7)),
            _Drop(_Item('e_bos_progress_item_t0', 20)),
        ))
        # Reactivation doubles gold and Wisdom. The boss progress effect would
        # add 5%, but the event catalog has already applied it on this path.
        effects = (
            _entry(self.effect_ids['buff_reactivation_campaign_instant_01']),
            _entry(self.effect_ids['buff_progressdrop_boss_bonus'], values=(0.05,)),
        )
        updated = apply_status_drop_amounts(
            DEFAULT_STATIC_DB_PATH,
            effects,
            generation,
            excluded_template_ids=('e_bos_progress_item_t0',),
        )
        self.assertEqual(
            tuple(drop.item.amount for drop in updated.drops),
            (20, 14, 20),
        )

    def test_status_snapshot_filters_both_deadline_clocks(self):
        persistent_live = _entry(1)
        persistent_expired = _entry(2)
        persistent_permanent = _entry(3)
        runtime_live = _entry(4)
        runtime_expired = _entry(5)
        runtime_permanent = _entry(6)
        additional = _entry(7)
        entries = snapshot_active_status_entries(
            {
                10: persistent_live,
                11: persistent_expired,
                12: persistent_permanent,
            },
            {10: 101.0, 11: 99.0},
            {
                20: (runtime_live, 51.0),
                21: (runtime_expired, 49.0),
                22: (runtime_permanent, None),
            },
            {30: additional},
            now_epoch=100.0,
            now_monotonic=50.0,
        )
        self.assertEqual(
            tuple(entry.effect_id for entry in entries),
            (1, 3, 4, 6, 7),
        )


if __name__ == '__main__':
    unittest.main()
