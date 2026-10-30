"""Authoritative player health/resource reconciliation and regeneration."""

from __future__ import annotations

import os
import sqlite3
import random
from functools import lru_cache

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.gameplay.status_modifier_runtime import (
    evaluate_active_status_modifiers, load_status_modifier_catalog,
    snapshot_player_modifier_sources,
)


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


@lru_cache(maxsize=8)
def _resource_bound_effect_ids(database_path: str) -> frozenset[int]:
    """Effects a skill starts with ``OFF:noskillresource``.

    CONTRACT: they end once their holder's skill resource is spent.
    EVIDENCE (data): IronDwarf's skill_irondwarf_transform (and the CNY 2026
    set's set_cny2026_dwarf_iron_trans) is ``D:600 ... OFF:noskillresource``;
    its StartModifiers fill the steam (CurrSkillResource 1.0) and drain it
    (SkillResourceRegeneration -5 against the dwarf's +3), so the
    transformation lasts as long as its steam.
    FAILURE: without it the transformation stayed 600 s, the steam at 0 and
    draining: "steam does not regenerate" after an Iron Dwarf.
    UNVERIFIED: the retail client's OFF trigger was not traced; read from the
    column's own name and the drain it pairs with.
    """

    ids = set()
    try:
        with sqlite3.connect(
            f"file:{Path(database_path).as_posix()}?mode=ro", uri=True
        ) as database:
            rows = database.execute(
                "SELECT UserStatusEffects FROM _Template_Skill "
                "WHERE UserStatusEffects LIKE '%noskillresource%'"
            ).fetchall()
            for (column,) in rows:
                for raw in str(column or "").split(";"):
                    parts = [part.strip() for part in raw.split(",")]
                    if not any(
                        part.casefold() == "off:noskillresource"
                        for part in parts[1:]
                    ):
                        continue
                    row = database.execute(
                        "SELECT rowid - 1 FROM _Template_StatusEffect "
                        "WHERE Id=? LIMIT 1",
                        (parts[0],),
                    ).fetchone()
                    if row is not None:
                        ids.add(int(row[0]))
    except (OSError, sqlite3.Error):
        return frozenset()
    return frozenset(ids)


class PlayerResourceService:
    """Own live vitality limits, skill payments, and resource regeneration."""

    @staticmethod
    def _end_player_runtime_effects(s: Session, matches, *, label: str) -> bool:
        """End the player's own running effects that ``matches`` selects.

        Each one is dropped with its timers, removed on the client with the
        retail no-details cleanup (also relayed to the viewers) and the vitals
        it changed are reconciled. Returns whether any ended.
        """

        from dso_server.services.item_set_programs import stop_item_set_effects
        tracked = getattr(s, "gameplay_item_set_programs", {})
        set_ids = {entry.effect_id for instance_id, (entry, _expiry)
                   in tuple(s.gameplay_runtime_status_effects.items())
                   if instance_id in tracked and matches(entry)}
        if set_ids:
            stop_item_set_effects(s, set_ids, server_tick=s.gameplay_chase_server_tick)
        timers = []
        removed = []
        with s.flow_lock:
            now = time.perf_counter()
            for instance_id, (entry, expires_at) in tuple(
                s.gameplay_runtime_status_effects.items()
            ):
                if expires_at is not None and expires_at <= now:
                    continue
                if not matches(entry):
                    continue
                s.gameplay_runtime_status_effects.pop(instance_id, None)
                for prefix in ("player-status-tick", "runtime-effect-expire"):
                    timer = s.gameplay_projectile_timers.pop(
                        (prefix, int(instance_id)), None
                    )
                    if timer is not None:
                        timers.append(timer)
                removed.append(entry)
            server_tick = int(s.gameplay_chase_server_tick) & 0xFFFFFFFF
            if removed:
                s.gameplay_disabled_feature_cache = None
        for timer in timers:
            timer.cancel()
        if not removed:
            return bool(set_ids)
        value = StatusEffect(False, tuple(
            CharacterService._consumable_cleanup_entry(
                entry, server_tick=server_tick
            )
            for entry in removed
        ))
        s.send_reliable_ordered(
            encode_status_effect_batch(value, actor_id=LOCAL_PLAYER_ACTOR_ID),
            track=True,
            label=label,
        )
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, ("status_effects", (value,)))
        CharacterService._refresh_player_combat_vitals(
            s, inventory_changed=False
        )
        return True

    @staticmethod
    def _end_resource_bound_effects(s: Session) -> bool:
        """End the effects that last only while the skill resource does."""

        combat = s.gameplay_combat_state
        path = (
            combat.encounter.static_database_path
            if combat is not None else DEFAULT_STATIC_DB_PATH
        )
        ids = _resource_bound_effect_ids(str(path))
        if not ids:
            return False
        return CharacterService._end_player_runtime_effects(
            s,
            lambda entry: int(entry.effect_id) in ids,
            label="player-resource-exhausted",
        )

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
            modifier_sources: tuple[object, ...], now: float,
            on_commit=None) -> bool:
        """Atomically pay/gain one cast and publish its exact resource pool.

        ``on_commit`` marks a successful debit before publication can fail.
        It runs under combat.lock and must only update caller-local state;
        callbacks must not take another lock or perform I/O.
        """

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
            class_name = CLASS_NAMES.get(int(character.class_id), "mage")
            level = max(1, int(getattr(character, "level", 1)))
        cost_fraction, gain_fraction = (
            combat.player_skill_resource_fractions(
                skill_index,
                modifier_sources=tuple(modifier_sources),
            )
        )
        # The client prices skills against the class's base pool for its
        # level, not the maximum pets and wisdom raise (see
        # LocalCombatState.apply_player_skill_resource).
        resource_pool = prime_player_combat_database(
            combat.encounter.static_database_path
        ).base_resource(class_name, level)
        if os.getenv("DSO_SKILL_TRACE", "0").strip().lower() in {"1", "true", "yes"}:
            # What a cast pays and which active effects priced it (Banner of
            # War's ResourceCost -100% is effect 5530).
            effect_ids = sorted({int(getattr(m, "effect_id", -1)) for m in modifier_sources
                                 if getattr(m, "effect_id", None) is not None})
            print(f"[skill-resource] skill {skill_index} cost {cost_fraction:.3f} "
                  f"gain {gain_fraction:.3f} pool {resource_pool:g} effects {effect_ids}")
        if cost_fraction <= 0.0 and gain_fraction <= 0.0:
            return True
        with combat.lock:
            accepted, current, _maximum, cost, gain = (
                combat.apply_player_skill_resource(
                    actor_id,
                    cost_fraction=cost_fraction,
                    gain_fraction=gain_fraction,
                    now=now,
                    resource_pool=resource_pool,
                )
            )
            if accepted and on_commit is not None:
                on_commit()
            current_health = combat.player_health.get(
                int(actor_id), [max(1, int(round(inventory.health_points)))]
            )[0]
        with s.flow_lock:
            if s.gameplay_inventory is not inventory:
                return False
            inventory.secondary_resource = float(current)
        try:
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
        finally:
            # A rejected cast, a generator, or a free cast never spent a
            # resource. Successful debits still proc if publication fails.
            if accepted and float(cost) > 0.0:
                from dso_server.services.item_set_events import dispatch_item_set_event
                dispatch_item_set_event(
                    s, "costskillresource",
                    server_tick=int(s.gameplay_chase_server_tick),
                    event_seed=int(skill_index),
                )
        if accepted:
            CharacterService._request_account_save(s, "skill resource changed")
            if float(current) <= 1e-6:
                CharacterService._end_resource_bound_effects(s)
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
        with s.flow_lock:
            modifiers = snapshot_player_modifier_sources(s)
        bonuses = evaluate_active_status_modifiers(
            load_status_modifier_catalog(combat.encounter.static_database_path),
            modifiers,
        )
        # Called only after ingress/cooldown/resource validation accepts the
        # cast. Roll once here, never during a preflight or a rejected cast.
        cost = bonuses.essence_cost(
            int(cost), roll=random.random() if bonuses.ignore_essence_cost > 0 else 1.0,
        )
        if cost <= 0:
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

        definitions = tuple(
            definition for definition in definitions
            if "currskillresource" in definition.modifier_kinds
        )
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
            # The health beside it is the combat's own: monster blows do not
            # write inventory.health_points, so that mirror still held the
            # health from before them and every tick refilled the HUD.
            # FAILURE: "la barre de vie ... le client la met a 100% alors que
            #   je prends des degats".
            combat = target_session.gameplay_combat_state
            live = None
            if combat is not None:
                with combat.lock:
                    live = combat.player_health.get(actor_id)
                    live = None if live is None else int(live[0])
            with target_session.flow_lock:
                inventory = target_session.gameplay_inventory
                if inventory is None:
                    continue
                inventory.secondary_resource = current_resource
                if live is not None:
                    inventory.health_points = float(live)
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
            if current_resource <= 1e-6:
                CharacterService._end_resource_bound_effects(target_session)


__all__ = ["PlayerResourceService"]
