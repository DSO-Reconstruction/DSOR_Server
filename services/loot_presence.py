"""Per-player world-loot allocation in a shared map instance."""

from __future__ import annotations

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
from dso_server.gameplay.mortis_arena import (
    MAP_ID as MORTIS_MAP, UNBOUND as MORTIS_FINAL_FORM, FINAL_GEM_REWARDS,
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
        if event_catalog is not None:
            loot_cache = (
                getattr(self, "world_loot_cache", None)
                or prime_world_loot_cache(encounter.static_database_path)
            )
            profile = loot_cache.monster_profiles.get(monster.template_id)
            event_drops = event_catalog.collectible_drops(
                map_id=event_map_id,
                player_level=max(1, int(player_level)),
                monster_rank=(
                    profile.rank_kind
                    if profile is not None else "normal"
                ),
                participation=event_participation,
                status_effects=status_effects,
                rng=generator,
                map_aliases=event_map_aliases,
                monster_template_id=str(monster.template_id),
                progress=event_progress,
            )
            event_loot = event_catalog.event_loot_drops(
                map_id=event_map_id,
                map_aliases=event_map_aliases,
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
            map_loot = event_catalog.map_loot_drops(
                map_id=event_map_id,
                map_aliases=event_map_aliases,
                class_name=class_name,
                monster_template_id=str(monster.template_id),
                monster_rank=(
                    profile.rank_kind
                    if profile is not None else "normal"
                ),
                monster_level=max(1, int(monster.level)),
                rng=generator,
                map_difficulty=map_difficulty,
                privileged_difficulty_multiplier=float(
                    difficulty_factors[1]
                ),
            )
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

        # Database/cache work is pure and can take milliseconds. Generate with
        # provisional IDs, then reserve one exact map-global range in a tiny
        # world-lock transaction.
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
        return generation
