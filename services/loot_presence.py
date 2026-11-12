"""Per-player world-loot allocation in a shared map instance."""

from __future__ import annotations

import math
from dataclasses import replace
from threading import Lock

from dso_server.runtime.context import *
from dso_server.protocol.inventory_codec import owned_inventory_items
from dso_server.gameplay.status_modifier_runtime import (
    apply_status_drop_amounts,
    snapshot_active_status_entries,
)
from dso_server.runtime.transport import Session
from dso_server.protocol.membership_protocol import (
    PREMIUM_MEMBERSHIP,
    membership_is_active,
)
from dso_server.gameplay.event_system import (
    _map_key,
    _tokens,
    completed_achievement_ids,
)
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.protocol.consumable_protocol import load_consumable_catalog
from dso_server.gameplay.mortis_arena import (
    MAP_ID as MORTIS_MAP, UNBOUND as MORTIS_FINAL_FORM, FINAL_GEM_REWARDS,
)


_BOSS_INGREDIENT_REGION_CACHE: dict[
    tuple[str, tuple[int, int]], dict[str, frozenset[str]]
] = {}
_BOSS_INGREDIENT_REGION_CACHE_LOCK = Lock()
_DRAGAN_FIRST_CLEAR_CLAIM_KEY = "dragan_great_hall_first_boss_reward"
_DRAGAN_CLASS_SET_ITEMS = {
    # The template format below already inserts ``2h_`` before this suffix.
    # Store only the weapon name here so we resolve the real static item IDs.
    "warrior": ("sword", "warrior"),
    "mage": ("staff", "mage"),
    "ranger": ("bow", "ranger"),
    "dwarf": ("rifle", "dwarf"),
}
_EASTER_DARAN_TEMPLATE = "e_easter_01_dun_boss_troll_01"
_DARK_DWARF_HEIST_MAPS = frozenset({
    'e_z0110_dun',
    'e_viscanium_submarine_01',
})
_DARK_DWARF_DROP_POTIONS = (
    'darkdwarf_potion_hour_s',
    'darkdwarf_potion_hour_l',
    'darkdwarf_potion_times_s',
    'darkdwarf_potion_times_l',
)


def _apply_dark_dwarf_boss_drop_bonus(
    generation,
    *,
    database_path,
    status_effects,
    event_catalog,
    map_ids,
    monster_rank,
):
    """Double Heist recipes and Viscanium Ore from bosses under Forgefyre."""

    if generation is None or str(monster_rank or '').casefold() not in {
        'boss', 'worldboss',
    }:
        return generation
    if not any(
        _map_key(value) in _DARK_DWARF_HEIST_MAPS for value in map_ids
    ):
        return generation
    if (
        event_catalog is None
        or 'e_ide_challenge_01' not in event_catalog.active_template_ids()
    ):
        return generation

    consumables = load_consumable_catalog(database_path)
    potion_effect_ids = {
        definition.effect_id
        for item_id in _DARK_DWARF_DROP_POTIONS
        if (definition := consumables.definition(item_id)) is not None
    }
    if not potion_effect_ids.intersection(
        int(effect.effect_id) for effect in status_effects
    ):
        return generation

    updated = []
    changed = False
    for drop in generation.drops:
        template_id = str(drop.item.template_id).casefold()
        item_category = str(drop.item_category or '').casefold()
        if not (
            template_id == 'e_ide_02_challenge_progress_02'
            or template_id == 'e_ide_02_chest_key_recipe_01'
            or item_category in {'recipeitem', 'recipeconsumable'}
        ):
            updated.append(drop)
            continue
        if template_id == 'e_ide_02_challenge_progress_02':
            updated.append(replace(
                drop,
                item=replace(
                    drop.item,
                    amount=max(1, int(drop.item.amount)) * 2,
                ),
            ))
        else:
            # Recipe items are authored with MaxStackSize=1. Emit a second
            # item instead of building an invalid two-count recipe stack.
            stack_amount = max(1, int(drop.item.amount))
            copies = stack_amount * 2
            updated.extend(
                replace(drop, item=replace(drop.item, amount=1))
                for _ in range(copies)
            )
        changed = True
    return replace(generation, drops=tuple(updated)) if changed else generation


def _dragan_great_hall_first_clear_items(class_name: str) -> tuple[str, ...]:
    """All Dragan set 01/02 items for a class, plus pets through Uncommon."""

    class_key = str(class_name or "").casefold()
    weapon_type = _DRAGAN_CLASS_SET_ITEMS.get(class_key)
    if weapon_type is None:
        return ()
    weapon, item_class = weapon_type
    return (
        *(
            f"{item_class}_unique_2h_{weapon}_dragan_set_01_{variant}"
            for variant in ("base", "fire", "lightning", "poison")
        ),
        f"{item_class}_unique_backitem_dragan_set_01",
        f"{item_class}_unique_shoulders_dragan_set_01",
        f"{item_class}_unique_torso_dragan_set_01",
        f"{item_class}_unique_boots_dragan_set_02",
        f"{item_class}_unique_gloves_dragan_set_02",
        f"{item_class}_unique_helmet_dragan_set_02",
        f"{item_class}_unique_ring_dragan_set_02",
        "pet_dragan_common",
        "pet_dragan_uncommon",
    )


def _confirm_dragan_great_hall_first_clear(
    service, session, generation,
) -> None:
    """Persist the one-time claim only after its drops were published."""

    if not bool(getattr(generation, "dragan_first_clear_reward", False)):
        return
    with session.flow_lock:
        progress = dict(
            getattr(session, "gameplay_event_progress", {}) or {}
        )
        progress[_DRAGAN_FIRST_CLEAR_CLAIM_KEY] = {
            "claimed": True,
            "delivered": True,
        }
        session.gameplay_event_progress = progress
        session.gameplay_dragan_first_clear_pending = False
    CharacterService._request_account_save(
        service, session,
        "Dragan Great Hall first boss rewards delivered",
    )


def mystic_cube_drop_is_eligible(session: Session) -> bool:
    """Premium and Deluxe each grant the Mystic Cube drop on their own.

    The stored Premium expiry is absolute and already excludes the Deluxe
    pause, so checking it directly is enough.
    """

    return membership_is_active(
        int(getattr(session, 'gameplay_membership_type', 0)),
        bool(getattr(session, 'gameplay_membership_active', False)),
        str(getattr(session, 'gameplay_membership_expires_at', '') or ''),
    ) or membership_is_active(
        PREMIUM_MEMBERSHIP,
        True,
        str(
            getattr(
                session, 'gameplay_membership_premium_expires_at', '',
            )
            or ''
        ),
    )


class LootPresenceService:
    """Allocate collision-free, character-bound drops for map occupants."""

    @staticmethod
    def _confirm_dragan_great_hall_first_clear(
        service, session, generation,
    ) -> None:
        _confirm_dragan_great_hall_first_clear(
            service, session, generation,
        )

    @staticmethod
    def _separate_world_loot_positions(
            session: Session, drops, *, minimum_spacing: float | None = None):
        """Spread new ground drops away from coordinates already in use.

        Callers hold ``gameplay_world_loot_position_lock`` until they publish
        the drops. Several Circus bosses can die at the same arena center, so
        retaining each generated item's original coordinate would stack the
        complete reward pile on top of older loot.
        """

        map_id = _map_key(getattr(session, "gameplay_map", ""))
        is_bgh_map = map_id.startswith("e_bgh_boss_")
        is_circus_arena = map_id == "e_bos_arena_01"
        if not (is_bgh_map or is_circus_arena or minimum_spacing is not None):
            return tuple(drops)
        with session.flow_lock:
            occupied = [
                tuple(float(axis) for axis in drop.item.ground_position)
                for drop in session.gameplay_world_loot.values()
                if getattr(drop.item, "ground_position", None) is not None
            ]
        placed = []
        minimum_spacing = max(
            0.1,
            float(minimum_spacing)
            if minimum_spacing is not None
            else (0.9 if is_bgh_map else 0.7),
        )
        for drop in drops:
            position = getattr(drop.item, "ground_position", None)
            if position is None:
                continue
            origin = tuple(float(axis) for axis in position)

            def is_free(candidate):
                return all(
                    math.hypot(candidate[0] - other[0], candidate[2] - other[2])
                    >= minimum_spacing
                    for other in (*occupied, *placed)
                )

            selected = origin
            if not is_free(selected):
                found = False
                for ring in range(1, 13):
                    radius = (
                        0.7 + ring * 0.25
                        if is_bgh_map
                        else 0.85 + ring * 0.55
                    )
                    for step in range(16):
                        angle = (step + (ring % 2) * 0.5) * (math.tau / 16)
                        candidate = (
                            origin[0] + math.cos(angle) * radius,
                            origin[1],
                            origin[2] + math.sin(angle) * radius,
                        )
                        if is_free(candidate):
                            selected = candidate
                            found = True
                            break
                    if found:
                        break
            drop.item.ground_position = selected
            placed.append(selected)
        return tuple(drops)

    @staticmethod
    def _guaranteed_worldboss_ingredient(
        loot_cache,
        event_catalog,
        *,
        monster_template_id: str,
        map_ids: tuple[str, ...],
        map_difficulty: int,
    ) -> tuple[str, int] | None:
        """Return the difficulty-specific ingredient owned by this boss/map.

        Ingredient item regions name their boss or event map. Match those
        regions against both the monster and its current map so bosses whose
        actor template omits Region still resolve correctly.
        """

        monster_regions = set(
            event_catalog.monster_regions.get(
                str(monster_template_id).casefold(), frozenset()
            )
        )
        map_regions = set()
        for map_id in map_ids:
            key = _map_key(map_id)
            if key:
                map_regions.update(event_catalog.map_regions.get(key, ()))
        scopes = monster_regions | map_regions
        if not scopes:
            return None

        cache_key = (
            str(loot_cache.database_path),
            tuple(loot_cache.database_revision),
        )
        with _BOSS_INGREDIENT_REGION_CACHE_LOCK:
            ingredient_families = _BOSS_INGREDIENT_REGION_CACHE.get(cache_key)
            if ingredient_families is None:
                suffix = "_rarity_01"
                ingredient_families = {}
                for item_id, row in loot_cache.items_by_id.items():
                    item_id = str(item_id)
                    if not item_id.startswith("boss_ingredient_") or not item_id.endswith(suffix):
                        continue
                    family = item_id[len("boss_ingredient_"):-len(suffix)]
                    regions = frozenset(
                        token.casefold()
                        for token in _tokens(row.get("Region"))
                        if token.casefold() != "none"
                    )
                    if regions:
                        ingredient_families[family] = regions
                _BOSS_INGREDIENT_REGION_CACHE[cache_key] = ingredient_families

        ingredient_families = {
            family: regions
            for family, regions in ingredient_families.items()
            if regions & scopes
        }

        # Prefer a match explicitly carried by the monster if a map happens
        # to host more than one event/boss family.
        direct_matches = {
            family for family, regions in ingredient_families.items()
            if regions & monster_regions
        }
        matches = direct_matches or set(ingredient_families)
        if len(matches) != 1:
            return None

        family = next(iter(matches))
        rarity_tier = min(5, max(0, int(map_difficulty)) // 2 + 1)
        item_id = f"boss_ingredient_{family}_rarity_{rarity_tier:02d}"
        if item_id not in loot_cache.items_by_id:
            return None
        return item_id, 1

    def _generate_world_loot_for_session(
            self,
            session: Session,
            *,
            encounter,
            monster,
            class_name: str,
            player_level: int,
            position: tuple[float, float, float],
            position_projector):
        """Allocate one map-global, client-safe runtime item-id range.

        Inventory item managers index objects by the low runtime item bits.
        A counter owned by each connection lets simultaneous kills by two
        players generate the same ids before either presence callback updates
        its peer. The shared map lock serializes allocation across every
        session and advances the map high-water mark before it is released.
        """

        if (
            str(getattr(encounter, "map_id", "")).casefold() == MORTIS_MAP
            and str(getattr(monster, "template_id", "")).casefold()
            != MORTIS_FINAL_FORM
        ):
            # Mortis is a multi-form encounter. Intermediate forms and the
            # transition adds are not kill-reward sources; only the unbound
            # final form may allocate loot.
            return None

        is_rocketmen_dungeon = (
            str(getattr(encounter, "map_id", "")).casefold()
            == "e_nye_dungeon_01"
        )

        if (
            str(getattr(encounter, "map_id", "")).casefold()
            == "e_hal_01_castle_dun"
            and str(getattr(monster, "template_id", "")).casefold() in {
                "e_hal_01_boss_gwenfara_phase_01",
                "e_hal_01_boss_gwenfara_phase_02",
            }
        ):
            # The first two deaths change the encounter; only the final
            # Gwenfara form owns the map's boss rewards.
            return None

        if (
            str(getattr(encounter, "map_id", "")).casefold()
            == "e_cny_boss_02"
            and str(getattr(monster, "template_id", "")).casefold()
            != "boss_cny_beast"
        ):
            # Altered Kingdom's guardians and both add waves are encounter
            # phases, not loot sources. Keep the final Nian kill on the normal
            # loot path so its event rewards can be rolled below.
            return None

        with session.flow_lock:
            map_id = str(session.gameplay_map or "")
            event_map_id = str(
                getattr(
                    session,
                    'gameplay_map_access_group_id',
                    None,
                )
                or getattr(session, 'gameplay_map_access_id', None)
                or map_id
            )
            map_access_id = str(
                getattr(session, 'gameplay_map_access_id', None)
                or event_map_id
            )
            event_map_aliases = (
                map_id,
                str(getattr(session, 'gameplay_map_access_id', None) or ""),
            )
            is_easter_daran = (
                _map_key(encounter.map_id) in {
                    "e_easter_dun_01", "i0010_physical_big_dun",
                }
                and str(monster.template_id).casefold()
                == _EASTER_DARAN_TEMPLATE
            )
            loot_map_id = (
                "e_easter_dun_01" if is_easter_daran else event_map_id
            )
            loot_map_aliases = (
                (*event_map_aliases, "e_easter_dun_01")
                if is_easter_daran else event_map_aliases
            )
            world_instance_key = (
                CharacterService._gameplay_instance_key(session)
            )
            points = tuple(session.gameplay_talent_category_points)
            wisdom_eligible = (
                len(points) == 4
                and int(points[1]) < int(session.gameplay_wisdom_maximum_xp)
            )
            map_difficulty = max(
                int(session.gameplay_map_difficulty),
                int(getattr(session, 'gameplay_scaling_difficulty', 0)),
            )
            active_costume = str(
                getattr(session, "gameplay_active_costume", "") or ""
            )
            # Premium and Deluxe each grant the cube drop on their own.
            member_mystic_cube_eligible = mystic_cube_drop_is_eligible(
                session
            )
            socket_evaluation = session.gameplay_socket_effect_evaluation
            event_participation = dict(
                session.gameplay_event_participation
            )
            # Copy-on-write (see player_persistence): the reference is a
            # stable snapshot. Fixed-page bars drop by the player's page.
            event_progress = dict(
                getattr(session, 'gameplay_event_progress', None) or {}
            )
            completed_achievements = completed_achievement_ids(
                getattr(session, 'gameplay_achievements', None)
            )
            status_effects = snapshot_active_status_entries(
                session.gameplay_persistent_status_effects,
                session.gameplay_timed_status_effect_expiries,
                session.gameplay_runtime_status_effects,
                session.gameplay_item_set_effects,
                session.gameplay_socket_effects,
            )
            event_catalog = session.gameplay_event_catalog

        # Event rolls share the generation RNG so one kill is deterministic
        # under tests and requires no static-database work on the hot path.
        generator = random.Random()
        difficulty_unscaled_drop_templates = frozenset()
        if event_catalog is None and hasattr(self, "static_database_path"):
            event_catalog = CharacterService._event_catalog(self)
        dragan_first_clear_items = ()
        if (
            event_catalog is not None
            and "dragan_event_03" in event_catalog.active_template_ids()
            and str(monster.template_id).casefold() in {
                "boss_dragan", "e_dra_03_boss_dragan",
            }
            and "e_dra_03_dun_02" in {
                _map_key(map_id), _map_key(event_map_id),
            }
        ):
            with session.flow_lock:
                progress_snapshot = dict(
                    getattr(session, "gameplay_event_progress", {}) or {}
                )
                claim_state = progress_snapshot.get(
                    _DRAGAN_FIRST_CLEAR_CLAIM_KEY, {}
                )
                claimed = bool(
                    isinstance(claim_state, dict)
                    and claim_state.get("claimed")
                )
                first_clear_items = (
                    _dragan_great_hall_first_clear_items(class_name)
                )
                if (
                    claimed
                    and isinstance(claim_state, dict)
                    and not claim_state.get("delivered")
                    and first_clear_items
                ):
                    inventory = getattr(session, "gameplay_inventory", None)
                    owned_templates = {
                        str(item.template_id).casefold()
                        for item in owned_inventory_items(inventory)
                    } if inventory is not None else set()
                    # Older code consumed the one-time claim as soon as loot
                    # was generated. If the account owns none of the promised
                    # set/pet items, treat that legacy claim as undelivered.
                    claimed = any(
                        item_id.casefold() in owned_templates
                        for item_id in first_clear_items
                    )
                pending = bool(getattr(
                    session, "gameplay_dragan_first_clear_pending", False,
                ))
                if not claimed and not pending:
                    dragan_first_clear_items = first_clear_items
                    if dragan_first_clear_items:
                        session.gameplay_dragan_first_clear_pending = True
        if event_catalog is not None:
            loot_cache = (
                getattr(self, "world_loot_cache", None)
                or prime_world_loot_cache(encounter.static_database_path)
            )
            profile = loot_cache.monster_profiles.get(monster.template_id)
            event_drops = event_catalog.collectible_drops(
                map_id=loot_map_id,
                player_level=max(1, int(player_level)),
                monster_rank=(
                    profile.rank_kind
                    if profile is not None else "normal"
                ),
                participation=event_participation,
                status_effects=status_effects,
                rng=generator,
                map_aliases=loot_map_aliases,
                monster_template_id=str(monster.template_id),
                progress=event_progress,
                completed_achievements=completed_achievements,
                map_difficulty=map_difficulty,
                active_costume=active_costume,
            )
            event_loot = event_catalog.event_loot_drops(
                map_id=loot_map_id,
                map_aliases=loot_map_aliases,
                player_level=max(1, int(player_level)),
                monster_template_id=str(monster.template_id),
                monster_rank=(
                    profile.rank_kind
                    if profile is not None else "normal"
                ),
                monster_level=max(1, int(monster.level)),
                participation=event_participation,
                rng=generator,
                map_difficulty=map_difficulty,
            )
            difficulty_factors = loot_cache.difficulty_drop_factors.get(
                int(map_difficulty), (1.0, 1.0)
            )
            heart_lava_chance_bonus = 0.0
            if status_effects:
                status_effect_ids = load_status_runtime_catalog(
                    encounter.static_database_path
                ).effect_ids
                active_effect_ids = {
                    int(effect.effect_id) for effect in status_effects
                }
                if status_effect_ids.get("Lava_ess_potion_drop_200_effect") in active_effect_ids:
                    heart_lava_chance_bonus = 2.0
                if status_effect_ids.get("Lava_ess_potion_drop_500_effect") in active_effect_ids:
                    heart_lava_chance_bonus = max(
                        heart_lava_chance_bonus, 5.0,
                    )
            map_loot = event_catalog.map_loot_drops(
                map_id=loot_map_id,
                map_aliases=loot_map_aliases,
                class_name=class_name,
                monster_template_id=str(monster.template_id),
                monster_rank=(
                    profile.rank_kind
                    if profile is not None else "normal"
                ),
                monster_level=max(1, int(monster.level)),
                rng=generator,
                map_difficulty=map_difficulty,
                active_costume=active_costume,
                privileged_difficulty_multiplier=float(
                    difficulty_factors[1]
                ),
                drop_chance_bonus_by_item=(
                    {"item_ingredient_mercenary_heartlava": heart_lava_chance_bonus}
                    if heart_lava_chance_bonus else {}
                ),
            )
            if profile is not None and profile.rank_kind.casefold() == "worldboss":
                ingredient = None
                if (
                    str(map_access_id).casefold().startswith("e_hal_01_castle_dun_")
                    and str(monster.template_id).casefold()
                    == "e_hal_01_boss_gwenfara_phase_03"
                ):
                    # The access preview specifies the exact crafting tier;
                    # its first three difficulties share rarity 01.
                    ingredient_id = next((
                        item_id for item_id in
                        loot_cache.map_reward_items_by_access.get(map_access_id, ())
                        if item_id.startswith("boss_ingredient_halloween_rarity_")
                    ), None)
                    if ingredient_id in loot_cache.items_by_id:
                        ingredient = (ingredient_id, 1)
                elif not str(map_access_id).casefold().startswith(
                    "e_hal_01_castle_dun_"
                ):
                    ingredient = self._guaranteed_worldboss_ingredient(
                        loot_cache,
                        event_catalog,
                        monster_template_id=str(monster.template_id),
                        map_ids=(
                            map_id,
                            event_map_id,
                            map_access_id,
                            *event_map_aliases,
                        ),
                        map_difficulty=map_difficulty,
                    )
                if ingredient is not None:
                    map_loot = tuple(
                        drop for drop in map_loot
                        if drop.item_id.casefold() != ingredient[0].casefold()
                    )
                    event_loot = (*event_loot, ingredient)
            # A guaranteed map-owned progress stack replaces the ordinary
            # scheduled-event collectible roll for that same item. Otherwise
            # a boss kill can award the configured 1300 plus a second random
            # progress stack from the event-wide drop chance.
            map_progress_items = frozenset(
                drop.item_id.casefold() for drop in map_loot
                if drop.item_id.casefold() == "e_ful_01_progress_item_01"
            )
            if map_progress_items:
                event_drops = tuple(
                    (item_id, amount) for item_id, amount in event_drops
                    if str(item_id).casefold() not in map_progress_items
                )
            difficulty_unscaled_drop_templates = frozenset(
                drop.item_id
                for drop in map_loot
                if not drop.scale_stack_amount_with_difficulty
            )
            event_loot = (
                *event_loot,
                *((drop.item_id, drop.amount) for drop in map_loot),
            )
        else:
            # Lightweight protocol tests and deliberately minimal embedded
            # hosts have no event catalog; preserve the allocation hot path.
            event_drops = ()
            event_loot = ()

        # Creepy Castle's capture wisp itself must award the unchastened soul
        # consumed by its bottle prop. The retail regional drop is not present
        # in the reconstructed event catalog, so attach it to the wisp's own
        # lethal loot transition instead of relying on its incident minion.
        if (
            str(getattr(encounter, "map_id", "")).casefold()
            == "e_hal_01_castle_dun"
            and str(getattr(monster, "template_id", "")).casefold() in {
                "e_hal_01_castle_quest_will_o_the_wisp_01",
                "e_hal_01_castle_quest_will_o_the_wisp_02",
                "e_hal_01_castle_quest_will_o_the_wisp_03",
            }
        ):
            event_loot = (*event_loot, ("e_hal_01_quest_ghostkey", 1))

        if dragan_first_clear_items:
            event_loot = (
                *event_loot,
                *((item_id, 1) for item_id in dragan_first_clear_items),
            )

        if (
            str(getattr(encounter, "map_id", "")).casefold() == MORTIS_MAP
            and str(getattr(monster, "template_id", "")).casefold()
            == MORTIS_FINAL_FORM
            and status_effects
        ):
            dragon_bone_effect_id = load_status_runtime_catalog(
                encounter.static_database_path
            ).effect_ids.get("dragon_bone_drop_buff")
            if dragon_bone_effect_id is not None and any(
                int(effect.effect_id) == int(dragon_bone_effect_id)
                for effect in status_effects
            ):
                # Normal 0, Painful 1, Excruciating 2, Fatal 3,
                # Infernal/PW Infernal 4/5, Merciless/PW Merciless 6/7,
                # and Bloodshed/PW Bloodshed 8/9.
                dragon_bones_by_difficulty = (
                    1, 2, 3, 4, 5, 5, 15, 15, 25, 25,
                )
                amount = dragon_bones_by_difficulty[max(
                    0, min(
                        int(map_difficulty),
                        len(dragon_bones_by_difficulty) - 1,
                    ),
                )]
                event_loot = (*event_loot, ("dragon_bones", amount))

        # Database/cache work is pure and can take milliseconds. Generate with
        # provisional IDs, then reserve one exact map-global range in a tiny
        # world-lock transaction.
        try:
            generation = generate_monster_loot(
                encounter.static_database_path,
                monster.template_id,
                class_name=class_name,
                player_level=max(1, int(player_level)),
                monster_level=max(1, int(monster.level)),
                position=position,
                next_item_id=0,
                rng=generator,
                force_drops=FORCE_LOOT_DROPS_FOR_TESTING,
                wisdom_eligible=wisdom_eligible,
                member_mystic_cube_eligible=member_mystic_cube_eligible,
                map_difficulty=map_difficulty,
                map_access_id=map_access_id,
                extra_boss_gems=int(
                    getattr(socket_evaluation, "boss_bonus_gems", 0)
                ),
                extra_boss_clovers=int(
                    getattr(socket_evaluation, "boss_bonus_clovers", 0)
                ),
                guaranteed_gem_templates=(
                    FINAL_GEM_REWARDS
                    if str(encounter.map_id).casefold() == MORTIS_MAP
                    and str(monster.template_id).casefold() == MORTIS_FINAL_FORM
                    else ()
                ),
                additional_instant_consumables=event_drops,
                position_projector=position_projector,
                additional_event_loot=event_loot,
                difficulty_unscaled_drop_templates=(
                    difficulty_unscaled_drop_templates
                ),
                # Replace Daran's authored Brush chance with the configured
                # 2% map-loot roll and suppress the rune itself. The map-loot
                # Brush is appended after these native drops are filtered.
                excluded_base_drop_templates=(
                    frozenset({"ingredient_rune_resistance_ice_fire_light"})
                    if is_easter_daran else frozenset()
                ),
                excluded_base_drop_template_prefixes=(
                    ("unique_gem_resistance_ice_fire_light",)
                    if is_easter_daran else ()
                ),
            )
            generation = apply_socket_drop_amounts(
                socket_evaluation,
                generation,
            )
            generation = apply_status_drop_amounts(
                encounter.static_database_path,
                status_effects,
                generation,
                # EventCatalog has already resolved these exact item modifiers,
                # including stack limits and parameter substitution. Excluding
                # them here prevents a progress buff being applied twice while
                # generic money/item bonuses still affect the rest of the kill.
                excluded_template_ids=(
                    template_id for template_id, _amount in event_drops
                ),
            )
            generation = _apply_dark_dwarf_boss_drop_bonus(
                generation,
                database_path=encounter.static_database_path,
                status_effects=status_effects,
                event_catalog=event_catalog,
                map_ids=(
                    map_id, event_map_id, map_access_id, *event_map_aliases
                ),
                monster_rank=(
                    profile.rank_kind if profile is not None else 'normal'
                ),
            )
            if is_rocketmen_dungeon:
                # Rocketmen's ordinary currencies/equipment stay disabled,
                # while its explicitly requested 35% Oiltastic Mixture drop
                # and the normal health globes remain available.
                generation = replace(
                    generation,
                    drops=tuple(
                        drop for drop in generation.drops
                        if (
                            str(drop.item.template_id).casefold()
                            in {"e_nye_keyitem_01"}
                            or "healthglobe" in str(
                                drop.item.template_id
                            ).casefold()
                        )
                    ),
                )
        except Exception:
            if dragan_first_clear_items:
                with session.flow_lock:
                    session.gameplay_dragan_first_clear_pending = False
            raise
        reservation_size = max(
            len(generation.drops),
            int(generation.next_item_id),
        )
        with session.flow_lock:
            session_next_item_id = int(session.gameplay_next_item_id)
            inventory = session.gameplay_inventory
            occupied_indices = {
                int(item.item_id) & GAME_ITEM_INDEX_MASK
                for item in owned_inventory_items(inventory)
            } if inventory is not None else set()
            occupied_indices.update(
                int(item_id) & GAME_ITEM_INDEX_MASK
                for item_id in session.gameplay_world_loot
            )
            occupied_indices.update(
                int(item_id) & GAME_ITEM_INDEX_MASK
                for item_id in session.gameplay_announced_world_item_ids
            )
            with self.gameplay_world_lock:
                first_item_id = max(
                    session_next_item_id,
                    int(self.gameplay_world_next_item_ids.get(
                        world_instance_key, RUNTIME_ITEM_ID_BASE
                    )),
                )
                item_ids = []
                candidate = first_item_id
                for _drop in generation.drops:
                    while (
                        candidate & GAME_ITEM_INDEX_MASK
                    ) in occupied_indices:
                        candidate += 1
                    if candidate > 0xFFFFFFFF:
                        if dragan_first_clear_items:
                            session.gameplay_dragan_first_clear_pending = False
                        raise ValueError(
                            "client item-id space is exhausted"
                        )
                    item_ids.append(candidate)
                    occupied_indices.add(
                        candidate & GAME_ITEM_INDEX_MASK
                    )
                    candidate += 1
                next_item_id = max(
                    candidate,
                    first_item_id + reservation_size,
                )
                self.gameplay_world_next_item_ids[
                    world_instance_key
                ] = max(
                    int(self.gameplay_world_next_item_ids.get(
                        world_instance_key, RUNTIME_ITEM_ID_BASE
                    )),
                    next_item_id,
                )
                # Retain a physical-map aggregate for diagnostics and legacy
                # introspection. Allocations read only the exact tuple key above.
                self.gameplay_world_next_item_ids[map_id] = max(
                    int(self.gameplay_world_next_item_ids.get(
                        map_id,
                        RUNTIME_ITEM_ID_BASE,
                    )),
                    next_item_id,
                )
                session.gameplay_next_item_id = max(
                    session_next_item_id,
                    next_item_id,
                )
        for drop, item_id in zip(generation.drops, item_ids):
            drop.item.item_id = item_id
        try:
            generation = replace(generation, next_item_id=next_item_id)
        except TypeError:
            # Lightweight protocol tests use SimpleNamespace generations.
            generation.next_item_id = next_item_id
        if dragan_first_clear_items:
            generation = replace(
                generation, dragan_first_clear_reward=True,
            )
        return generation
