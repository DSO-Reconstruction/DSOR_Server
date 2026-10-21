"""Saved status effects survive a client-data update that renumbers them."""

from types import MappingProxyType

from dso_server.gameplay.status_runtime_catalog import (
    StatusRuntimeCatalog,
    rebind_saved_status_effects,
)
from dso_server.protocol.gameplay_protocol import StatusEffectEntry


def _entry(effect_id, instance_id):
    return StatusEffectEntry(
        effect_id, instance_id, 0, 25, 0, 0, 25, 100, 1,
        False, False, True, True, (0.25, 0.0, 0.0, 0.0, 0.0),
    )


def _catalog(effect_ids):
    return StatusRuntimeCatalog(
        programs=MappingProxyType({}), groups=MappingProxyType({}),
        actor_variables=MappingProxyType({}),
        effect_ids=MappingProxyType(effect_ids),
        zero_duration_effect_ids=frozenset(),
        effect_names=MappingProxyType({v: k for k, v in effect_ids.items()}),
    )


def test_saved_effects_follow_their_name_into_a_renumbered_database():
    # The October 2026 data inserted rows: pet_jabbax moved from 504 to 507,
    # and 6075 became Sewers2026_Vstate_mage ("Venomous Form").
    october = _catalog({
        "pet_chicken_white": 504, "pet_jabbax": 507,
        "Sewers2026_Vstate_mage": 6075, "sb_pets_increased_hitpoints_01": 6397,
    })
    saved = (_entry(504, 10), _entry(6075, 11), _entry(9999, 12), _entry(504, 13))
    names = {10: "pet_jabbax", 11: "sb_pets_increased_hitpoints_01",
             12: "removed_from_the_game"}
    rebound = rebind_saved_status_effects(saved, names, october)
    assert [(e.field_04, e.effect_id) for e in rebound] == [
        (10, 507), (11, 6397),
        # No recorded name (an older save): the number is kept.
        (13, 504),
    ]
    assert rebound[0].values == saved[0].values
