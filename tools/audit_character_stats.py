"""Reproduce the Example Player 2 screenshot audit without opening the player database.

Run from the server directory: py -3.12-64 tools/audit_character_stats.py
The fixture retains equipment and effects, including saved admin bonuses.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import _state_from_document
from dso_server.protocol.talent_protocol import TalentDatabase
from dso_server.protocol.item_set_protocol import ItemSetDatabase
from dso_server.protocol.socket_effect_protocol import SocketEffectDatabase, apply_socket_drop_amounts
from dso_server.gameplay.stickerbook_state import reconcile_collector_effects
from dso_server.gameplay.status_modifier_runtime import (
    evaluate_active_status_modifiers, load_status_modifier_catalog, apply_status_drop_amounts,
)
from dso_server.gameplay.player_combat_stats import (
    _calculate_player_combat_stats, prime_player_combat_database,
)

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


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests/fixtures/player_stats_example_player.json"


def character_sources(document, *, database_path=DEFAULT_STATIC_DB_PATH):
    """Rebuild the persistent/talent/set/socket/collector inputs of map entry."""
    # The stat-only fixture intentionally omits balances and map bookkeeping.
    state = _state_from_document({"currencies": {}, "map_id": "b1201_pve", "next_item_id": 1, **document})
    talents = TalentDatabase(database_path)
    sources = list(state.status_effects)
    for category in state.talent_categories:
        for selection in category.selected_skills:
            sources.extend(talents.status_effect_entries(
                talents.definition(selection.resource_id), target_rank=selection.field_00,
                actor_id=1, first_instance_id=200000 + len(sources), server_tick=0,
            ))
    item_sets = ItemSetDatabase(database_path)
    sets = item_sets.evaluate_inventory(state.inventory, class_name=document["class_name"])
    sources.extend(item_sets.reconcile_effects(
        sets, {}, actor_id=1, next_instance_id=300000, server_tick=0,
    ).effects.values())
    sources.extend(sets.modifiers)
    socket_db = SocketEffectDatabase(database_path)
    sockets = socket_db.evaluate_inventory(state.inventory)
    sources.extend(socket_db.reconcile_effects(
        sockets, {}, actor_id=1, next_instance_id=400000, server_tick=0,
    ).effects.values())
    collector = reconcile_collector_effects(
        database_path, {entry.resource_id for entry in state.stickerbook_entries},
        {entry.field_04: entry for entry in sources if hasattr(entry, "effect_id")},
        actor_id=1, next_instance_id=500000, server_tick=0,
    )
    sources = tuple(collector.effects.values()) + tuple(
        entry for entry in sources if not hasattr(entry, "effect_id")
    )
    return state.inventory, sources, sockets


def evaluate_character(document, *, database_path=DEFAULT_STATIC_DB_PATH):
    database = prime_player_combat_database(database_path)
    inventory, sources, sockets = character_sources(document, database_path=database_path)
    pet_effect = database.status_effect_ids["pet_jabbax"]
    results = {}
    for pet in (False, True):
        active = tuple(entry for entry in sources if pet or getattr(entry, "effect_id", None) != pet_effect)
        stats = _calculate_player_combat_stats(
            database, class_name=document["class_name"], level=document["level"],
            inventory=inventory, modifier_sources=active,
        )
        result = asdict(stats)
        result.pop("skill_modifier_sources")
        result["block_value"] = stats.block_chance / database.progression_factor(document["level"], "BlockChanceFactor")
        result["health_regeneration_per_second"] = (
            stats.maximum_health * stats.health_regeneration_fraction + stats.health_regeneration_absolute
        ) / stats.health_regeneration_interval
        result["displayed_health_regeneration_per_second"] = result["health_regeneration_per_second"]
        result["mana_regeneration_per_second"] = (
            stats.maximum_resource * stats.resource_regeneration_fraction + stats.resource_regeneration_absolute
        ) / stats.resource_regeneration_interval
        bonuses = evaluate_active_status_modifiers(load_status_modifier_catalog(database_path), active)
        result["status_bonuses"] = asdict(bonuses)
        result["drop_award_from_10000"] = {}
        for key in ("money_rc", "money_vc", "money_gc", "materi_fragment_01"):
            generation = apply_socket_drop_amounts(sockets, _Generation((_Drop(_Item(key, 10000)),)))
            generation = apply_status_drop_amounts(database_path, active, generation)
            result["drop_award_from_10000"][key] = generation.drops[0].item.amount
        results["pet_on" if pet else "pet_off"] = result
    return results


def comparison_report(document, results):
    rows = []
    def row(name, screenshot, getter, *, notes="", result=None):
        off, on = (str(getter(results[key])) for key in ("pet_off", "pet_on"))
        rows.append(dict(stat=name, screenshot=str(screenshot), server_pet_off=off, server_pet_on=on,
                         result=result or ("match (pet off)" if off == str(screenshot) else "mismatch"), notes=notes))
    row("Level", 100, lambda r: document["level"])
    inventory, _, _ = character_sources(document)
    equipped = {item.item_id: item for item in inventory.items}
    levels = [equipped[int(item_id)].item_level for item_id, slots in inventory.equipment_slots.items()
              for slot in slots if 0 <= slot <= 12]
    average = sum(levels) / len(levels)
    row("Average item level", 110, lambda r: math.floor(average + .5), notes=f"Equipment slots 0-12 average {average}, including both slots of the two-handed weapon.")
    row("Base damage", 694684, lambda r: math.floor(r["minimum_damage"] + .5), notes="One-point residual; native float arithmetic/order not yet reproduced exactly.")
    row("Attacks per second", "19.509", lambda r: f'{r["attack_speed"]:.3f}', notes="Raw sheet speed; animation timing quantizes this to 12.5.")
    row("Critical value", "321054 (80.00%)", lambda r: f'{math.floor(r["critical_rating"]+.5)} ({r["critical_hit_chance"]*100:.2f}%)')
    row("Mana", 160, lambda r: r["maximum_resource"])
    row("Mana regeneration", "12.00", lambda r: f'{r["mana_regeneration_per_second"]:.2f}')
    row("Movement speed", "18.000", lambda r: f'{r["movement_speed"]+r["movement_bonus_speed"]:.3f}', notes="Includes saved admin movement bonus.")
    row("Health", 1042039, lambda r: r["maximum_health"], notes="One-point residual with pet off; pet on adds collection health bonus.")
    row("Health regeneration", "6981.98", lambda r: f'{r["displayed_health_regeneration_per_second"]:.2f}', notes="Passive healing follows the displayed regeneration bonus.")
    row("Block value", "56097 (14.14%)", lambda r: f'{math.floor(r["block_value"]+.5)} ({r["block_chance"]*100:.2f}%)')
    row("Armor", "71190 (58.07%)", lambda r: f'{math.floor(r["armor_rating"]+.5)} ({(1-dict(r["incoming_damage_multipliers"])["physical"])*100:.2f}%)', notes="Four-point residual in rating; displayed mitigation matches. Cause remains unverified.")
    for name, value, percent in (("fire",62533,54.89),("ice",119482,69.92),("lightning",62533,54.89),("darkmagic",78767,60.51),("poison",62533,54.89)):
        row(name.title(), f"{value} ({percent:.2f}%)", lambda r, n=name: f'{math.floor(dict(r["resistance_ratings"])[n]+.5)} ({(1-dict(r["incoming_damage_multipliers"])[n])*100:.2f}%)')
    row("XP gain", "100%", lambda r: f'{100*(1+r["status_bonuses"]["xp_gain"]["relative"]):.0f}%')
    row("Honor gain", "125%", lambda r: "100% base payout", result="unsupported bonus", notes="PvP settlement currently credits base honor. Native honor also uses separate event/twink/guild and user-benefit channels; source of this character's +25% is not yet verified.")
    for name, key, expected in (("Andermant", "money_rc",25),("Coin","money_vc",106),("Wisdom","money_gc",0),("Materi Fragment","materi_fragment_01",0)):
        row(name+" drop bonus", f"+{expected}%", lambda r,k=key: f'+{(r["drop_award_from_10000"][k]/10000-1)*100:.0f}%', notes="Evaluated socket then active-status drop bonuses on 10000 units.")
    return {
        "character_id":document["character_id"], "character_name":"Example Player 2",
        "snapshot_saved_at":"2026-09-30T00:02:04.526180+00:00",
        "method":"Offline frozen equipment/effects fixture, with selected talents, set/socket/collector effects and saved admin effects. Both pet states tested; no combat-only procs or live measurements.",
        "pet":"pet_frogman_epic / pet_jabbax: +25% critical rating and pet collection health while active",
        "comparison":rows, "raw_server_results":results,
        "remaining_differences":["Damage -1", "Health +1", "Armor -4", "Honor bonus is not applied by PvP payout"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture",type=Path,default=DEFAULT_FIXTURE)
    parser.add_argument("--output",type=Path,default=ROOT / "data/diagnostics/example_player_stats_comparison.json")
    args=parser.parse_args()
    document=json.loads(args.fixture.read_text(encoding="utf-8"))
    report=comparison_report(document,evaluate_character(document))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    with args.output.with_suffix(".csv").open("w",encoding="utf-8",newline="") as file:
        writer=csv.DictWriter(file,fieldnames=tuple(report["comparison"][0]))
        writer.writeheader();writer.writerows(report["comparison"])
    for item in report["comparison"]:
        print(f'{item["stat"]}: screenshot={item["screenshot"]}; pet off={item["server_pet_off"]}; pet on={item["server_pet_on"]}')
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
