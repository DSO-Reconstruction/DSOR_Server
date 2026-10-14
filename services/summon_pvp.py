"""PvP targeting and dispatch for autonomous player summons."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.gameplay.pvp_match_state import MatchPhase


class SummonPVPService:
    """Bridge shared summon simulation with mutually hostile player actors."""

    @staticmethod
    def _prepare_player_summon_cycle_context(
        combat,
        combat_targets,
        *,
        now: float,
    ):
        """Resolve session-owned summon inputs without simulation locks.

        Group-aware PvP hostility is resolved through the presence callback,
        which briefly takes a session ``flow_lock``. Calling that callback
        from the monster simulation's flow/combat critical section therefore
        self-deadlocked the owner, or formed an AB-BA cycle with a peer
        projectile worker.

        Snapshot only combat-owned identifiers under ``combat.lock``, then
        perform presence/social queries and player-stat calculation after
        releasing it. The simulation consumes these immutable dictionaries on
        the same tick and remains authoritative if a summon disappears between
        the two phases.
        """

        combat_targets = tuple(
            value for value in combat_targets
            if value[2].gameplay_combat_state is combat
        )
        sessions_by_actor = {
            int(actor_id): target_session
            for actor_id, _position, target_session in combat_targets
        }
        with combat.lock:
            owner_actor_ids = frozenset(
                int(summon.owner_actor_id)
                for summon in combat.player_summons.values()
            )
            due_damage_types = {}
            for pending in combat.pending_player_summon_attacks.values():
                if pending.attack.due_at > now:
                    continue
                summon = combat.player_summons.get(
                    int(pending.attack.attacker_actor_id)
                )
                if summon is None:
                    continue
                owner_actor_id = int(summon.owner_actor_id)
                for target_actor_id in pending.target_actor_ids:
                    due_damage_types[
                        (owner_actor_id, int(target_actor_id))
                    ] = pending.geometry.damage_type

        hostile_by_owner = {}
        lethal_by_owner = {}
        damage_locked = frozenset(
            actor_id for actor_id, target_session in sessions_by_actor.items()
            if CharacterService._player_victim_feature_locked(target_session, "damage")
        )
        for owner_actor_id in owner_actor_ids:
            owner_session = sessions_by_actor.get(owner_actor_id)
            if owner_session is None:
                continue
            hostile_by_owner[owner_actor_id] = frozenset(
                actor_id
                for actor_id, target_session in sessions_by_actor.items()
                if actor_id not in damage_locked
                and CharacterService._pvp_target_is_hostile(
                    owner_session, target_session
                )
            )
            lethal_by_owner[owner_actor_id] = frozenset(
                actor_id for actor_id in hostile_by_owner[owner_actor_id]
                if CharacterService._pvp_lethal_damage_allowed(
                    owner_session, sessions_by_actor[actor_id]
                )
            )

        player_damage_multipliers = {}
        target_stats_cache = {}
        for (owner_actor_id, target_actor_id), damage_type in (
            due_damage_types.items()
        ):
            if target_actor_id not in hostile_by_owner.get(
                owner_actor_id, ()
            ):
                continue
            target_session = sessions_by_actor.get(target_actor_id)
            if target_session is None:
                continue
            target_stats = target_stats_cache.get(target_actor_id)
            if target_stats is None:
                character = target_session.gameplay_character
                target_stats = calculate_player_combat_stats(
                    combat.encounter.static_database_path,
                    cache_owner=target_session,
                    class_name=CLASS_NAMES.get(
                        int(getattr(character, "class_id", 0)), "mage"
                    ),
                    level=max(1, int(getattr(character, "level", 1))),
                    inventory=target_session.gameplay_inventory,
                    modifier_sources=(
                        CharacterService._active_skill_modifier_sources(
                            target_session
                        )
                    ),
                )
                target_stats_cache[target_actor_id] = target_stats
            player_damage_multipliers[target_actor_id] = (
                CharacterService._pvp_damage_type_multiplier(
                    target_stats, damage_type
                )
            )

        return hostile_by_owner, player_damage_multipliers, lethal_by_owner

    @staticmethod
    def _resolve_player_summon_cycle(
        combat,
        combat_targets,
        player_world_positions,
        *,
        server_tick: int,
        now: float,
        hostile_player_actor_ids_by_owner,
        player_damage_multipliers,
        lethal_player_actor_ids_by_owner=None,
    ):
        sessions_by_actor = {
            int(actor_id): target_session
            for actor_id, _position, target_session in combat_targets
        }
        hostile_by_owner = hostile_player_actor_ids_by_owner

        combat.resolve_due_player_summon_taunts(now=now)
        out_of_range_summons = combat.player_summons_outside_owner_range(
            player_world_positions
        )
        summon_attacks = combat.maybe_start_player_summon_attacks(
            player_world_positions,
            server_tick=server_tick,
            now=now,
            hostile_player_actor_ids_by_owner=hostile_by_owner,
        )

        summon_resolutions, support_applications = (
            combat.resolve_due_player_summon_attacks(
                now=now,
                player_positions=player_world_positions,
                server_tick=server_tick,
                hostile_player_actor_ids_by_owner=hostile_by_owner,
                player_damage_multipliers=player_damage_multipliers,
                lethal_player_actor_ids_by_owner=lethal_player_actor_ids_by_owner,
            )
        )
        pvp_resolutions = tuple(
            resolution
            for resolution in summon_resolutions
            if int(resolution.target_actor_id) in sessions_by_actor
        )
        monster_resolutions = tuple(
            resolution
            for resolution in summon_resolutions
            if int(resolution.target_actor_id) not in sessions_by_actor
        )
        return (
            out_of_range_summons,
            summon_attacks,
            monster_resolutions,
            pvp_resolutions,
            support_applications,
        )

    @staticmethod
    def _dispatch_pvp_summon_resolutions(
        combat, target_sessions, resolutions
    ) -> None:
        for resolution in resolutions:
            owner_session = target_sessions.get(
                int(resolution.source_actor_id)
            )
            target_session = target_sessions.get(
                int(resolution.target_actor_id)
            )
            if (
                owner_session is None
                or target_session is None
                or not CharacterService._pvp_target_is_hostile(
                    owner_session, target_session
                )
            ):
                continue
            # Another hit may already have ended this round before dispatch.
            if CharacterService._player_victim_feature_locked(target_session, "damage"):
                continue
            match_opponents = CharacterService._pvp_match_opponents(
                owner_session, target_session
            )
            resolution = replace(
                resolution,
                victim_status_effects=CharacterService._filter_player_status_effects(
                    target_session, resolution.victim_status_effects, pvp=True,
                ),
            )
            with target_session.flow_lock:
                if target_session.gameplay_inventory is not None:
                    target_session.gameplay_inventory.health_points = float(
                        resolution.current_health
                    )
            if owner_session.gameplay_presence_callback is not None:
                owner_session.gameplay_presence_callback(
                    owner_session, ("pvp_combat_resolution", resolution)
                )
            if match_opponents and owner_session.gameplay_match_callback is not None:
                owner_session.gameplay_match_callback(
                    owner_session, ("match_damage", int(resolution.damage))
                )
                if resolution.killed:
                    owner_session.gameplay_match_callback(
                        owner_session, ("match_kill", target_session)
                    )
            if resolution.killed:
                continue
            CharacterService._schedule_pvp_status_damage(
                owner_session,
                owner_session.gameplay_chase_generation,
                SimpleNamespace(
                    target_session=target_session,
                    target_actor_id=int(resolution.target_actor_id),
                    source_actor_id=int(resolution.source_actor_id),
                    skill_index=int(resolution.skill_index),
                    position=combat.encounter.coordinate_frame.world_to_wire(
                        resolution.position
                    ),
                    player_damage_range=(
                        float(resolution.damage),
                        float(resolution.damage),
                    ),
                    combat=combat,
                ),
                resolution.victim_status_effects,
            )

    @staticmethod
    def _pvp_actor_is_summon(owner, actor_id):
        return int(actor_id) in getattr(owner.gameplay_combat_state, "player_summon_health", {})

    @staticmethod
    def _pvp_actor_damage_locked(owner, actor_id):
        if not CharacterService._pvp_actor_is_summon(owner, actor_id):
            return CharacterService._player_victim_feature_locked(owner, "damage")
        if owner.gameplay_match is not None and owner.gameplay_match.phase != MatchPhase.RUNNING:
            return True
        combat = owner.gameplay_combat_state
        with combat.lock:
            summon = combat.player_summons.get(int(actor_id))
            if (summon is None or summon.owner_actor_id != owner.gameplay_remote_actor_id
                    or combat.player_summon_health.get(int(actor_id), [0])[0] <= 0):
                return True
            return any("damage" in combat.status_effect_disabled_features(effect.effect_id)
                       for effect in combat._active_monster_modifier_sources(actor_id))

    @staticmethod
    def _pvp_summon_stats(owner, actor_id):
        combat = owner.gameplay_combat_state
        def incoming_damage_multiplier(damage_type):
            with combat.lock:
                return combat.monster_incoming_damage_multiplier(actor_id, damage_type)
        return SimpleNamespace(incoming_damage_multiplier=incoming_damage_multiplier)

    @staticmethod
    def _filter_pvp_actor_effects(owner, actor_id, definitions):
        if not CharacterService._pvp_actor_is_summon(owner, actor_id):
            return CharacterService._filter_player_status_effects(owner, definitions, pvp=True)
        definitions = tuple(replace(effect, duration_ticks=effect.pvp_duration_ticks)
                            if effect.pvp_duration_ticks is not None else effect
                            for effect in definitions)
        combat = owner.gameplay_combat_state
        with combat.lock:
            return combat.accepted_monster_status_effects(actor_id, definitions, now=time.perf_counter())

    @staticmethod
    def _record_pvp_summon_effects(combat, actor_id, definitions):
        # Keep controls in the existing actor-status evaluator. Periodic damage
        # belongs to the PvP timers, which revalidate hostility and round state.
        now = time.perf_counter()
        combat._record_monster_status_effects(actor_id, definitions, now=now)
        for effect in definitions:
            combat.monster_status_damage.pop((int(actor_id), effect.resource_id), None)
        combat._monster_action_lock_cache.pop(int(actor_id), None)
