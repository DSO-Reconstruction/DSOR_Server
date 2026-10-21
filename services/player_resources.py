"""Authoritative player health/resource reconciliation and regeneration."""

from __future__ import annotations

import os

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


def _reconciled_pool_value(
        previous_current: float, previous_maximum: float,
        new_maximum: float, *, minimum: float) -> float:
    """Preserve fullness across a max change; never turn a bonus into healing."""

    bounded_maximum = max(float(minimum), float(new_maximum))
    old_maximum = max(float(minimum), float(previous_maximum))
    old_current = max(float(minimum), float(previous_current))
    if old_current >= old_maximum:
        return bounded_maximum
    return min(bounded_maximum, old_current)


class PlayerResourceService:
    """Own live vitality limits, skill payments, and resource regeneration."""

    @staticmethod
    def _refresh_player_combat_vitals(
            s: Session, *, inventory_changed: bool = True) -> None:
        """Refresh maxima/recovery and clamp both live pools."""

        combat = s.gameplay_combat_state
        character = s.gameplay_character
        inventory = s.gameplay_inventory
        if character is None or inventory is None:
            return
        character_id = int(getattr(character, "character_id", 0))
        if combat is None and character_id <= 0:
            # An actor-less inventory snapshot is not a live player and has
            # no valid ActorStatsUpdate target (used by offline tooling).
            return
        if inventory_changed:
            invalidate_player_combat_stats(s, inventory_changed=True)
        stats = calculate_player_combat_stats(
            combat.encounter.static_database_path
            if combat is not None else DEFAULT_STATIC_DB_PATH,
            cache_owner=s,
            class_name=CLASS_NAMES.get(int(character.class_id), "mage"),
            level=max(1, int(getattr(character, "level", 1))),
            inventory=inventory,
            modifier_sources=CharacterService._active_skill_modifier_sources(s),
        )
        CharacterService._refresh_admin_speed(s, stats=stats)
        actor_id = (
            s.gameplay_remote_actor_id
            or (
                CharacterService._remote_actor_id(character_id)
                if character_id > 0
                else (
                    combat.player_actor_id
                    if combat is not None else LOCAL_PLAYER_ACTOR_ID
                )
            )
        )
        if combat is None:
            previous_current = max(1, int(round(inventory.health_points)))
            previous_resource = max(
                0.0, float(inventory.secondary_resource)
            )
            current = min(
                int(stats.maximum_health),
                previous_current,
            )
            current_resource = min(
                float(stats.maximum_resource),
                previous_resource,
            )
            publish = (
                int(current) != previous_current
                or abs(float(current_resource) - previous_resource) > 1e-6
            )
        else:
            with combat.lock:
                old_health = combat.player_health.get(
                    int(actor_id),
                    [int(stats.maximum_health), int(stats.maximum_health)],
                )
                old_resource = combat.player_resource.get(
                    int(actor_id),
                    [float(stats.maximum_resource)] * 2,
                )
                previous = (
                    int(old_health[0]),
                    int(old_health[1]),
                    float(old_resource[0]),
                    float(old_resource[1]),
                )
                reconciled_health = _reconciled_pool_value(
                    previous[0],
                    previous[1],
                    stats.maximum_health,
                    minimum=0.0,
                )
                reconciled_resource = _reconciled_pool_value(
                    previous[2],
                    previous[3],
                    stats.maximum_resource,
                    minimum=0.0,
                )
                combat.register_player(
                    actor_id,
                    stats.maximum_health,
                    current_health=reconciled_health,
                    regeneration_fraction=stats.health_regeneration_fraction,
                    regeneration_absolute=stats.health_regeneration_absolute,
                    regeneration_interval=stats.health_regeneration_interval,
                    maximum_resource=stats.maximum_resource,
                    current_resource=reconciled_resource,
                    resource_regeneration_fraction=(
                        stats.resource_regeneration_fraction
                    ),
                    resource_regeneration_absolute=(
                        stats.resource_regeneration_absolute
                    ),
                    resource_regeneration_interval=(
                        stats.resource_regeneration_interval
                    ),
                    resource_regeneration_delay=(
                        stats.resource_regeneration_delay
                    ),
                )
                current, maximum = combat.player_health[int(actor_id)]
                current_resource, maximum_resource = (
                    combat.player_resource[int(actor_id)]
                )
            publish = previous != (
                int(current),
                int(maximum),
                float(current_resource),
                float(maximum_resource),
            )
        with s.flow_lock:
            if s.gameplay_inventory is not inventory:
                return
            inventory.health_points = float(current)
            inventory.secondary_resource = float(current_resource)
        if publish:
            s.send_reliable_ordered(
                encode_actor_stats_update_batch(
                    ActorStatsUpdate(current, current_resource),
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                ),
                track=True,
                label="player-vitals-reconciled",
            )

    @staticmethod
    def _apply_player_skill_resource(
            s: Session, combat, *, skill_index: int,
            modifier_sources: tuple[object, ...], now: float) -> bool:
        """Atomically pay/gain one cast and publish its exact resource pool."""

        with s.flow_lock:
            character = s.gameplay_character
            inventory = s.gameplay_inventory
            if character is None or s.gameplay_combat_state is not combat:
                return False
            # Production skill ingress always follows InventoryInfo. Minimal
            # protocol harnesses have no authoritative resource pool to debit.
            if inventory is None:
                return True
            actor_id = (
                s.gameplay_remote_actor_id
                or CharacterService._remote_actor_id(character.character_id)
            )
        cost_fraction, gain_fraction = (
            combat.player_skill_resource_fractions(
                skill_index,
                modifier_sources=tuple(modifier_sources),
            )
        )
        if os.getenv("DSO_SKILL_TRACE", "0").strip().lower() in {"1", "true", "yes"}:
            # What a cast pays and which active effects priced it (Banner of
            # War's ResourceCost -100% is effect 5530).
            effect_ids = sorted({int(getattr(m, "effect_id", -1)) for m in modifier_sources
                                 if getattr(m, "effect_id", None) is not None})
            print(f"[skill-resource] skill {skill_index} cost {cost_fraction:.3f} "
                  f"gain {gain_fraction:.3f} effects {effect_ids}")
        if cost_fraction <= 0.0 and gain_fraction <= 0.0:
            return True
        with combat.lock:
            accepted, current, _maximum, cost, gain = (
                combat.apply_player_skill_resource(
                    actor_id,
                    cost_fraction=cost_fraction,
                    gain_fraction=gain_fraction,
                    now=now,
                )
            )
            current_health = combat.player_health.get(
                int(actor_id), [max(1, int(round(inventory.health_points)))]
            )[0]
        with s.flow_lock:
            if s.gameplay_inventory is not inventory:
                return False
            inventory.secondary_resource = float(current)
        s.send_reliable_ordered(
            encode_actor_stats_update_batch(
                ActorStatsUpdate(current_health, current),
                actor_id=LOCAL_PLAYER_ACTOR_ID,
            ),
            track=True,
            label=(
                "player-skill-resource"
                if accepted else "player-skill-resource-rejected"
            ),
        )
        if accepted:
            CharacterService._request_account_save(s, "skill resource changed")
        else:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected skill index "
                f"{int(skill_index)}: resource={current:.0f}, "
                f"cost={cost:.0f}, gain={gain:.0f}"
            )
        return bool(accepted)

    @staticmethod
    def _consume_active_essence(
            s: Session, combat, *, skill_index: int) -> bool:
        """Spend the active essence's ``AmmoCost`` for one accepted cast.

        Only the essence in the active ammunition slot burns; the parked one
        waits for the switch key. A stack smaller than the cost is used up
        rather than blocking the cast, matching the client's own bookkeeping.
        When the stack is gone its allocation is dropped, the combat stats are
        rebuilt so the damage factor and ammunition type stop applying, and
        the change is saved eagerly; ordinary decrements ride the coalesced
        save. Battlegrounds are exempt: essences do nothing there.

        Returns whether anything was spent.
        """

        if not ESSENCE_CONSUMPTION_ENABLED or combat is None:
            return False
        if getattr(s, "gameplay_match", None) is not None:
            return False
        cost = combat.player_skill_ammo_cost(skill_index)
        if cost <= 0:
            return False
        with s.flow_lock:
            inventory = s.gameplay_inventory
            character = s.gameplay_character
        if inventory is None or character is None:
            return False
        essence_id = next(
            (
                item_id
                for item_id, slots in active_equipment_slots(inventory).items()
                if ACTIVE_AMMUNITION_SLOT in slots
            ),
            None,
        )
        if essence_id is None:
            return False
        essence = next(
            (item for item in inventory.items if int(item.item_id) == essence_id),
            None,
        )
        if essence is None or int(essence.amount) <= 0:
            return False
        spent = min(int(cost), int(essence.amount))
        try:
            updated, delta, depleted = consume_equipped_stack(
                inventory, essence_id, spent
            )
        except ValueError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] could not spend essence "
                f"{essence.template_id!r}: {exc}"
            )
            return False
        with s.flow_lock:
            # Another inventory mutation won the race; keep this cast free
            # rather than spending from a snapshot that is no longer live.
            if s.gameplay_inventory is not inventory:
                return False
            s.gameplay_inventory = updated
        packet, packet_bits = encode_server_command_batch(
            (
                ServerCommand(
                    INVENTORY_INFO_COMMAND, delta, LOCAL_PLAYER_ACTOR_ID
                ),
            ),
            include_terminator=True,
        )
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="essence-consumed"
        )
        if depleted:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] essence {essence.template_id!r} "
                f"used up by skill index {int(skill_index)}"
            )
            # The damage factor and ammunition type came from this item.
            CharacterService._refresh_player_combat_vitals(s)
            CharacterService._request_account_save(
                s, "essence depleted", flush=True
            )
        else:
            CharacterService._request_account_save(s, "essence consumed")
        return True

    @staticmethod
    def _apply_player_status_resource(s: Session, definitions) -> None:
        """Apply immediate CurrSkillResource programs from user effects."""

        definitions = tuple(definitions)
        if not definitions:
            return
        with s.flow_lock:
            combat = s.gameplay_combat_state
            character = s.gameplay_character
            inventory = s.gameplay_inventory
            if combat is None or character is None or inventory is None:
                return
            actor_id = (
                s.gameplay_remote_actor_id
                or CharacterService._remote_actor_id(character.character_id)
            )
        with combat.lock:
            resource = combat.player_resource.get(int(actor_id))
            if resource is None:
                return
            current, maximum = resource
            delta = combat.player_status_resource_delta(
                definitions, maximum_resource=maximum
            )
            if abs(delta) <= 1e-6:
                return
            current = min(float(maximum), max(0.0, float(current) + delta))
            resource[0] = current
            resource_now = time.perf_counter()
            combat.last_player_resource_change_at[int(actor_id)] = resource_now
            combat.last_player_resource_regeneration_at[
                int(actor_id)
            ] = resource_now
            current_health = combat.player_health.get(
                int(actor_id), [max(1, int(round(inventory.health_points)))]
            )[0]
        with s.flow_lock:
            if s.gameplay_inventory is not inventory:
                return
            inventory.secondary_resource = float(current)
        s.send_reliable_ordered(
            encode_actor_stats_update_batch(
                ActorStatsUpdate(current_health, current),
                actor_id=LOCAL_PLAYER_ACTOR_ID,
            ),
            track=True,
            label="player-status-resource",
        )

    @staticmethod
    def _apply_base_resource_regeneration(
            target_sessions, resource_regeneration) -> None:
        """Publish class resource regeneration/decay without combat text."""

        latest_by_actor = {
            int(actor_id): float(current)
            for actor_id, current, _maximum, _delta in resource_regeneration
        }
        for actor_id, current_resource in latest_by_actor.items():
            target_session = target_sessions.get(actor_id)
            if target_session is None:
                continue
            with target_session.flow_lock:
                inventory = target_session.gameplay_inventory
                if inventory is None:
                    continue
                inventory.secondary_resource = current_resource
                # A resource tick must not republish the pre-Kill inventory
                # snapshot as a heal while the client is showing a corpse.
                current_health = (
                    0 if target_session.gameplay_death_pending
                    else max(0, int(round(inventory.health_points)))
                )
            target_session.send_reliable_ordered(
                encode_actor_stats_update_batch(
                    ActorStatsUpdate(current_health, current_resource),
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                ),
                track=True,
                label="passive-resource-regeneration",
            )


__all__ = ["PlayerResourceService"]
