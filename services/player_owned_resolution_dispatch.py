"""Batched publication of autonomous player-owned combat results."""

from __future__ import annotations

from dso_server.runtime.context import *


class PlayerOwnedResolutionDispatchService:
    """Publish DoT, summon, aura, and reflection hits by owner and tick."""

    @staticmethod
    def _remove_managed_player_aura_effects(s, definitions) -> None:
        """End a location-owned buff and its healing when its owner leaves."""
        entries = []
        timers = []
        with s.flow_lock:
            for definition in definitions:
                running = CharacterService._running_player_status_effect(s, definition.effect_id, now=time.perf_counter())
                if running is None:
                    continue
                instance_id, entry = running
                s.gameplay_runtime_status_effects.pop(instance_id, None)
                entries.append(replace(entry, has_details=False, values=()))
                for key in (("player-status-tick", instance_id), ("runtime-effect-expire", instance_id)):
                    timer = s.gameplay_projectile_timers.pop(key, None)
                    if timer is not None:
                        timers.append(timer)
        for timer in timers:
            timer.cancel()
        if entries:
            value = StatusEffect(False, tuple(entries))
            s.send_reliable_ordered(
                encode_status_effect_batch(value, actor_id=LOCAL_PLAYER_ACTOR_ID),
                track=True, label="player-left-managed-aura",
            )
            if s.gameplay_presence_callback is not None:
                s.gameplay_presence_callback(s, ("status_effects", (value,)))
            CharacterService._refresh_player_combat_vitals(s, inventory_changed=False)

    @staticmethod
    def _dispatch_location_aura_applications(
            target_sessions, applications) -> None:
        """Send same-tick ground-aura receivers in multi-actor batches.

        The shared status encoder retains one command/actor context and the
        session-local ADD-versus-refresh cache for every target. Its MTU
        chunking therefore preserves wire semantics without producing a
        targets-by-viewers datagram burst.
        """

        applications = tuple(applications)
        if not applications:
            return
        for application in applications:
            wearer = target_sessions.get(int(application.target_actor_id))
            if wearer is None:
                continue
            if application.remove:
                CharacterService._remove_managed_player_aura_effects(wearer, application.status_effects)
            else:
                CharacterService._send_player_skill_status_effects(
                    wearer, application.status_effects, server_tick=application.server_tick,
                    label="player-managed-aura-status-effects",
                    force_fresh_effect_ids={effect.effect_id for effect in application.status_effects},
                )
        actor_applications = tuple(
            (
                int(application.target_actor_id),
                None,
                tuple(application.status_effects),
            )
            for application in applications
            if int(application.target_actor_id) not in target_sessions and not application.remove
        )
        server_tick = max(
            int(application.server_tick)
            for application in applications
        )
        viewers = {
            id(target_session): target_session
            for target_session in target_sessions.values()
        }
        for target_session in viewers.values():
            CharacterService._send_socket_actor_applications(
                target_session,
                actor_applications,
                server_tick=server_tick,
                label="player-location-aura-status-effects",
            )

    @staticmethod
    def _dispatch_player_owned_resolutions(
            combat, target_sessions, resolutions) -> None:
        """Publish one tick's autonomous damage as an ordered transaction.

        Direct casts coalesce equal-deadline target impacts. Autonomous
        player-owned damage must retain that property: one area effect can
        touch many actors in the same 40 ms simulation frame, and publishing
        each result independently multiplies presence scans and reliable UDP
        datagrams by both target and viewer count.

        Life leech, loot ownership, and result ordering are owner scoped.
        Loot is still rolled independently for every lethal target; only its
        world-state publication is consolidated.
        """

        grouped_by_owner = {}
        for resolution in tuple(resolutions):
            owner_actor_id = int(
                resolution.source_actor_id
                if resolution.source_actor_id is not None
                else resolution.attacker_actor_id
            )
            owner_session = target_sessions.get(owner_actor_id)
            if owner_session is None:
                continue
            owner_key = id(owner_session)
            group = grouped_by_owner.get(owner_key)
            if group is None:
                group = [owner_session, []]
                grouped_by_owner[owner_key] = group
            group[1].append(resolution)

        for owner_session, owner_values in grouped_by_owner.values():
            owner_resolutions = tuple(owner_values)
            CharacterService._apply_player_damage_healing(
                owner_session,
                owner_resolutions,
                label="player-owned-life-leech",
            )

            dispatches = []
            killed_monsters = []
            owner_encounter = owner_session.gameplay_monster_encounter
            for resolution in owner_resolutions:
                loot_generation = None
                if resolution.killed:
                    monster = combat.monsters.get(
                        resolution.target_actor_id
                    )
                    character = owner_session.gameplay_character
                    encounter = owner_encounter
                    if monster is not None and encounter is not None:
                        killed_monsters.append(monster)
                    if (
                        monster is not None
                        and character is not None
                        and encounter is not None
                    ):
                        try:
                            allocator = (
                                owner_session.gameplay_world_loot_allocator
                            )
                            if allocator is not None:
                                loot_generation = allocator(
                                    owner_session,
                                    encounter=encounter,
                                    monster=monster,
                                    class_name=CLASS_NAMES.get(
                                        int(character.class_id), "mage"
                                    ),
                                    player_level=max(
                                        1, int(character.level)
                                    ),
                                    position=resolution.position,
                                    position_projector=(
                                        lambda candidate, target_actor_id=int(
                                            resolution.target_actor_id
                                        ): combat.ground_loot_position(
                                            target_actor_id, candidate
                                        )
                                    ),
                                )
                        except Exception as exc:
                            print(
                                f"[{GAMEPLAY_SERVICE_NAME}] player-owned "
                                "periodic/summon loot generation failed for "
                                f"{monster.template_id!r}: "
                                f"{type(exc).__name__}: {exc}"
                            )
                label = (
                    "player-lethal-status-hit"
                    if resolution.killed
                    else "player-status-hit-monster"
                )
                dispatches.append((resolution, label, loot_generation))

            callback = owner_session.gameplay_presence_callback
            if callback is not None:
                event = (
                    (
                        "player_combat_resolution",
                        dispatches[0][0],
                        dispatches[0][1],
                        dispatches[0][2],
                    )
                    if len(dispatches) == 1
                    else (
                        "player_combat_resolutions",
                        tuple(dispatches),
                    )
                )
                callback(owner_session, event)
                if killed_monsters and owner_encounter is not None:
                    CharacterService._award_monster_kill_xp(
                        owner_session,
                        tuple(killed_monsters),
                        database_path=owner_encounter.static_database_path,
                    )
                    CharacterService._notify_monsters_killed(
                        owner_session, tuple(killed_monsters)
                    )
                continue

            CharacterService._send_combat_resolutions(
                owner_session,
                owner_resolutions,
                label=(
                    dispatches[0][1]
                    if len(dispatches) == 1
                    else "player-owned-hit-batch"
                ),
            )
            if killed_monsters and owner_encounter is not None:
                CharacterService._award_monster_kill_xp(
                    owner_session,
                    tuple(killed_monsters),
                    database_path=owner_encounter.static_database_path,
                )
                CharacterService._notify_monsters_killed(
                    owner_session, tuple(killed_monsters)
                )
            loot_generations = tuple(
                generation
                for _resolution, _label, generation in dispatches
                if generation is not None and generation.drops
            )
            if not loot_generations:
                continue
            with owner_session.flow_lock:
                owner_session.gameplay_next_item_id = max(
                    owner_session.gameplay_next_item_id,
                    *(
                        generation.next_item_id
                        for generation in loot_generations
                    ),
                )
                owner_session.gameplay_world_loot.update(
                    {
                        drop.item.item_id: drop
                        for generation in loot_generations
                        for drop in generation.drops
                    }
                )
            CharacterService._reconcile_world_loot_visibility(
                owner_session,
                label="world-loot-spawn",
            )


__all__ = ["PlayerOwnedResolutionDispatchService"]
