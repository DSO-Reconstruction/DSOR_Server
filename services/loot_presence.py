"""Per-player world-loot allocation in a shared map instance."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.gameplay.status_modifier_runtime import (
    apply_status_drop_amounts,
    snapshot_active_status_entries,
)
from dso_server.runtime.transport import Session
from dso_server.gameplay.mortis_arena import (
    MAP_ID as MORTIS_MAP, UNBOUND as MORTIS_FINAL_FORM, FINAL_GEM_REWARDS,
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
            session_next_item_id = int(session.gameplay_next_item_id)
            event_map_id = str(
                getattr(
                    session,
                    'gameplay_map_access_group_id',
                    None,
                )
                or getattr(session, 'gameplay_map_access_id', None)
                or map_id
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
            socket_evaluation = session.gameplay_socket_effect_evaluation
            event_participation = dict(
                session.gameplay_event_participation
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
            )
        else:
            # Lightweight protocol tests and deliberately minimal embedded
            # hosts have no event catalog; preserve the allocation hot path.
            event_drops = ()

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
            map_difficulty=map_difficulty,
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
        with self.gameplay_world_lock:
            first_item_id = max(
                session_next_item_id,
                int(self.gameplay_world_next_item_ids.get(
                    world_instance_key, RUNTIME_ITEM_ID_BASE
                )),
            )
            next_item_id = first_item_id + reservation_size
            self.gameplay_world_next_item_ids[
                world_instance_key
            ] = next_item_id
            # Retain a physical-map aggregate for diagnostics and legacy
            # introspection. Allocations read only the exact tuple key above.
            self.gameplay_world_next_item_ids[map_id] = max(
                int(self.gameplay_world_next_item_ids.get(
                    map_id,
                    RUNTIME_ITEM_ID_BASE,
                )),
                next_item_id,
            )
        for offset, drop in enumerate(generation.drops):
            drop.item.item_id = first_item_id + offset
        try:
            generation = replace(generation, next_item_id=next_item_id)
        except TypeError:
            # Lightweight protocol tests use SimpleNamespace generations.
            generation.next_item_id = next_item_id
        with session.flow_lock:
            session.gameplay_next_item_id = max(
                int(session.gameplay_next_item_id),
                next_item_id,
            )
        return generation
