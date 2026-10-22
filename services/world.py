"""Movement, actor discovery, world loot, and vicinity state."""

from __future__ import annotations

from contextlib import contextmanager
import threading
import time

from dso_server.protocol.consumable_protocol import load_instant_consumable_definition

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.gameplay.fractured_planes_layout import is_tower_hardware
from dso_server.gameplay.boss_encounter_policy import (
    APPROACH_BOSS_MAPS, boss_entrance_presentation, load_map_encounter_policy,
)
from dso_server.services import movement_validation

_PERSISTENT_SCRIPTED_ACTORS = frozenset({
    "a0001_champion_undead_mage_01",
    "a0005_champion_grimmag_01_q_waytogrimmagstone_03",
})
# Fixed tower hardware is part of the arena, not a discovery target. Power
# nodes and trap shooters have to stand where they were authored from the
# moment the player arrives, because their beams and auras are what the room
# geometry was designed around. Ordinary monsters keep the normal vicinity
# radius; these are announced map-wide and never culled.
_TOWER_MECHANISM_VISIBILITY_RADIUS = 100000.0
_FORTRESS_GRIMMAG_MAP = "a0005_start_fortress_dun"
_FORTRESS_GRIMMAG_TEMPLATE = "a0005_champion_grimmag_01_q_waytogrimmagstone_03"
_FORTRESS_GRIMMAG_VISIBILITY_ENTER_RADIUS = MONSTER_VISIBILITY_ENTER_RADIUS
_FORTRESS_GRIMMAG_VISIBILITY_EXIT_RADIUS = 85.0
_SMALL_CRYPT_MAP = "a0008_crypt_dun"
_SMALL_CRYPT_CAPTAIN_TEMPLATE = "a0008_normal_champion_crypt_undead"
_TORSROND_MAP = "b1101_pve"
_TORSROND_CAPTAIN_TEMPLATE = "b1101_s_soulless_norseman_q_soullessflood03"
_WILDFOREST_CRYPT_EXIT_MAP = "a0302_wildforest"
_WILDFOREST_NEFERTARI_GUARD_TEMPLATE = (
    "a0302_normal_s_undead_mage_q_cultpath01"
)
# The Wild Forest entrance is about 138 units from this Crypt Exit guardian.
# Announce it on entry so the authored actor is not missed by the client.
_WILDFOREST_GUARD_VISIBILITY_ENTER_RADIUS = 250.0
_WILDFOREST_GUARD_VISIBILITY_EXIT_RADIUS = 275.0
_WILDHERZ_CAVERNS_MAP = "a0303_wildcave"
_WILDHERZ_YACHAK_TEMPLATE = (
    "a0303_normal_g_beastman_stag_q_goddessreawakening05"
)
_TEGAN_GRAVE_MAP = "a0601_defiledsanctum"
_TEGAN_GRAVE_SMITH_TEMPLATE = (
    "a0601_normal_g_undead_mage_q_tegangravesmith"
)
_TEGAN_GRAVE_SMITH_VISIBILITY_ENTER_RADIUS = 120.0
_TEGAN_GRAVE_SMITH_VISIBILITY_EXIT_RADIUS = 135.0
_TIME_HARP_MAP = "c2302_dun"
_TIME_HARP_QUEST_MOB_TEMPLATES = frozenset({
    "c2302_normal_g_gorgon_q_templeofeternity_03a",
    "c2302_normal_g_gorgon_q_templeofeternity_03a_scale",
    "c2302_normal_g_gorgon_q_templeofeternity_03b",
    "c2302_normal_g_gorgon_q_templeofeternity_03b_scale",
    "c2302_normal_g_lobster_q_templeofeternity_03",
    "c2302_normal_g_lobster_q_templeofeternity_03_scale",
})


@contextmanager
def _rollback_uncommitted_world_pickup_heal(
        combat, actor_id, healed):
    '''Undo a speculative globe heal after item/generation validation fails.'''

    committed = [False]
    try:
        yield committed
    finally:
        if (
            not committed[0]
            and combat is not None
            and actor_id is not None
            and healed is not None
        ):
            previous, current, _maximum = healed
            recovered = max(0, int(current) - int(previous))
            if recovered > 0:
                with combat.lock:
                    health = combat.player_health.get(int(actor_id))
                    if health is not None:
                        maximum = max(1, int(health[1]))
                        health[0] = min(
                            maximum,
                            max(0, int(health[0]) - recovered),
                        )
                        if int(actor_id) == int(combat.player_actor_id):
                            combat.player_current_health = int(health[0])


class WorldService:
    """Movement, actor discovery, world loot, and vicinity state."""

    @staticmethod
    def _pending_entrance_actor_ids(s, combat):
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if combat is None or runtime is None or runtime.map_id.casefold() not in APPROACH_BOSS_MAPS:
            return frozenset()
        with runtime.lock:
            if "boss_spawn" in runtime.running:
                return frozenset()
        with combat.lock:
            return frozenset(
                actor_id for actor_id, monster in combat.monsters.items()
                if monster.rank_id.casefold().startswith("rank_boss")
            )

    @staticmethod
    def _is_tower_hardware_actor(encounter, combat, actor_id) -> bool:
        """Whether ``actor_id`` is fixed tower hardware on this map."""

        if encounter is None or combat is None:
            return False
        monster = combat.monsters.get(int(actor_id))
        if monster is None:
            return False
        return is_tower_hardware(
            encounter.static_database_path, encounter.map_id,
            monster.template_id,
        )

    @staticmethod
    def _monster_visibility_radius(encounter, combat, actor_id, *, exiting=False):
        """Return the visibility boundary for an actor on this map."""

        monster = (
            combat.monsters.get(int(actor_id))
            if combat is not None
            else None
        )
        if (
            monster is not None
            and encounter is not None
            and monster.rank_id.casefold().startswith("rank_boss")
            and load_map_encounter_policy(
                encounter.static_database_path, encounter.map_id
            ).is_boss_room
        ):
            # Arena entrances can be farther than the ordinary vicinity
            # radius. Do not discard a freshly published boss mid-entrance.
            return _TOWER_MECHANISM_VISIBILITY_RADIUS
        if (
            monster is not None
            and encounter is not None
            and is_tower_hardware(
                encounter.static_database_path, encounter.map_id,
                monster.template_id,
            )
        ):
            return _TOWER_MECHANISM_VISIBILITY_RADIUS
        if (
            encounter is not None
            and encounter.map_id.casefold() == _FORTRESS_GRIMMAG_MAP
            and monster is not None
            and monster.template_id.casefold() == _FORTRESS_GRIMMAG_TEMPLATE
        ):
            return (
                _FORTRESS_GRIMMAG_VISIBILITY_EXIT_RADIUS
                if exiting
                else _FORTRESS_GRIMMAG_VISIBILITY_ENTER_RADIUS
            )
        if (
            encounter is not None
            and encounter.map_id.casefold() == _WILDFOREST_CRYPT_EXIT_MAP
            and monster is not None
            and monster.template_id.casefold()
            == _WILDFOREST_NEFERTARI_GUARD_TEMPLATE
        ):
            return (
                _WILDFOREST_GUARD_VISIBILITY_EXIT_RADIUS
                if exiting
                else _WILDFOREST_GUARD_VISIBILITY_ENTER_RADIUS
            )
        if (
            encounter is not None
            and encounter.map_id.casefold() == _TEGAN_GRAVE_MAP
            and monster is not None
            and monster.template_id.casefold() == _TEGAN_GRAVE_SMITH_TEMPLATE
        ):
            return (
                _TEGAN_GRAVE_SMITH_VISIBILITY_EXIT_RADIUS
                if exiting
                else _TEGAN_GRAVE_SMITH_VISIBILITY_ENTER_RADIUS
            )
        return (
            MONSTER_VISIBILITY_EXIT_RADIUS
            if exiting
            else MONSTER_VISIBILITY_ENTER_RADIUS
        )

    @staticmethod
    def _monster_groups_within_radius(
            encounter, *, world_position, actor_ids, positions,
            radius: float, extra_groups=(), radius_for_actor=None):
        """Build runtime group subsets from individually-near actors.

        Spawn groups remain useful packet batches, but their center and most
        distant member are not visibility boundaries.  Treating them as such
        made one near member reveal a full group or made one distant member
        block every other member.  This helper applies the distance predicate
        per current actor transform, then retains groups only for encoding.
        """

        candidates = {int(actor_id) for actor_id in actor_ids}
        if not candidates:
            return ()
        player_x = float(world_position[0])
        player_z = float(world_position[2])
        groups = []
        authored = encounter.groups if encounter is not None else ()
        for original in (*authored, *extra_groups):
            selected = original.actor_id_set.intersection(candidates)
            if not selected:
                continue
            nearby = {
                actor_id
                for actor_id in selected
                for position in (positions.get(actor_id),)
                if (
                    position is not None
                    and (
                        (float(position[0]) - player_x) ** 2
                        + (float(position[2]) - player_z) ** 2
                    ) <= max(
                        0.0,
                        float(
                            radius_for_actor(actor_id)
                            if radius_for_actor is not None
                            else radius
                        ),
                    ) ** 2 + 1e-9
                )
            }
            if not nearby:
                continue
            runtime = original.runtime_subset(
                nearby, positions=positions
            )
            if runtime is not None:
                groups.append(runtime)
        return tuple(groups)

    @staticmethod
    def _new_monster_batch_for_viewer(s, combat, batch, *, label: str):
        """Strip NewMonster commands this client has already been sent.

        The client asserts outright on a duplicate actor registration, so a
        second NewMonster for a live actor id does not merely waste bandwidth -
        it fails ``GameWorld::NewMonster`` and leaves an actor without an
        effect module, which crashes the client on the next frame. Several
        paths reserve an id and encode a batch outside the session lock, so a
        reservation alone cannot prove the actor was never published. Keep an
        explicit sent ledger and drop repeats here, at the single choke point
        every monster spawn goes through.
        """

        if combat is None:
            return batch
        commands = tuple(batch.commands)
        startup_overrides = {}
        presentation_changed = False
        with s.flow_lock:
            if s.gameplay_combat_state is not combat:
                return batch
            sent = s.gameplay_new_monster_sent_actor_ids
            kept = []
            duplicates = []
            new_monsters = 0
            for command in commands:
                if isinstance(command, NewMonsterServerCommand):
                    new_monsters += 1
                    actor_id = int(command.value.actor_id)
                    if actor_id in sent:
                        duplicates.append(actor_id)
                        continue
                    if (
                        combat.encounter.map_id.casefold() in APPROACH_BOSS_MAPS
                        and command.value.rank.casefold().startswith("rank_boss")
                    ):
                        visual, duration = boss_entrance_presentation(
                            combat.encounter.static_database_path,
                            command.value.template_id,
                        )
                        move = getattr(s, "gameplay_last_move", None)
                        spawn_tick = int(getattr(s, "gameplay_chase_server_tick", 0) or (
                            move.start_tick if move is not None else 0
                        )) & 0xFFFFFFFF
                        command = NewMonsterServerCommand(replace(
                            command.value, template_id=visual, field_c4=spawn_tick,
                        ))
                        startup_overrides[actor_id] = duration
                        presentation_changed = True
                    sent.add(actor_id)
                kept.append(command)
        published_actor_ids = tuple(
            int(command.value.actor_id) for command in kept
            if isinstance(command, NewMonsterServerCommand)
        )
        if published_actor_ids:
            with combat.lock:
                combat.begin_monster_spawn(
                    published_actor_ids, startup_overrides=startup_overrides,
                )
        if duplicates:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] suppressed duplicate NewMonster for "
                f"actor(s) {sorted(set(duplicates))} on "
                f"{getattr(getattr(combat, 'encounter', None), 'map_id', '?')} "
                f"({label}); the client already created them"
            )
        tower_ids = tuple(
            int(command.value.actor_id)
            for command in kept
            if isinstance(command, NewMonsterServerCommand)
            and int(command.value.actor_id)
            in getattr(combat, "tower_beam_started_at", {})
        )
        if tower_ids:
            # Permanent monster status effects are serialized with StartTick=0.
            # The client therefore phases the rotating aura from the shared
            # server clock, even when this viewer receives the actor later.
            started_at = getattr(s, "gameplay_tower_beam_started_at", None)
            if started_at is None:
                started_at = {}
                s.gameplay_tower_beam_started_at = started_at
            for actor_id in tower_ids:
                started_at[actor_id] = 0.0
        if not duplicates and not presentation_changed:
            return batch
        if new_monsters and not any(
            isinstance(command, NewMonsterServerCommand) for command in kept
        ):
            # Every actor in this batch was already published, and the
            # permanent status effects travel with NewMonster. Nothing here
            # would create anything the client does not already have.
            return None
        if not kept:
            return None
        return MonsterSpawnBatch(tuple(kept))

    @staticmethod
    def _forget_published_monsters(s, actor_ids) -> None:
        """Release actor ids from the sent ledger once the client drops them.

        ``_new_monster_batch_for_viewer`` refuses to publish an actor twice
        because the client asserts on a duplicate registration. That ledger
        must shrink in lockstep with the client's own actor table, otherwise a
        monster that was culled and later re-enters the vicinity would never be
        created again and would stay invisible while still alive and blocking
        every clear gate. Call this on every path that sends a discard, in the
        same critical section that edits ``gameplay_spawned_monster_actor_ids``.
        """

        forgotten = tuple(int(actor_id) for actor_id in actor_ids)
        if not forgotten:
            return
        s.gameplay_new_monster_sent_actor_ids.difference_update(forgotten)

    @staticmethod
    def _monster_vicinity_state(combat, group):
        """Build retail's MonsterUpdate* -> ActorsEnterVicinity snapshot."""

        monsters_by_actor = {
            int(command.value.actor_id): command.value
            for command in group.spawn_batch.commands
            if isinstance(command, NewMonsterServerCommand)
        }
        with combat.lock:
            updates = tuple(
                MonsterUpdateServerCommand(
                    MonsterUpdate(
                        field_20=max(
                            0, int(combat.monster_health.get(actor_id, 0))
                        ),
                        field_28=max(
                            1, int(combat.monsters[actor_id].maximum_health)
                        ),
                        field_30=int(monsters_by_actor[actor_id].field_98),
                        field_34=0,
                        field_38=100,
                    ),
                    actor_id,
                )
                for actor_id in group.actor_ids
                if actor_id in monsters_by_actor
            )
        return MonsterVicinityBatch(
            updates=updates,
            vicinity=group.vicinity.value,
            vicinity_actor_id=group.vicinity.actor_id,
        )

    @staticmethod
    def _send_monster_vicinity(s: Session, combat, group, *, label: str) -> None:
        # Dense event waves exceed one datagram. Keep MonsterUpdate and
        # ActorsEnterVicinity in one ordered transaction, fragmented to the
        # client's MTU, or a lost oversized packet blocks all later replies.
        packet, bits = encode_monster_vicinity_batch(
            CharacterService._monster_vicinity_state(combat, group)
        )
        CharacterService._send_world_loot_state_batch(s, packet, bits, label=label)

    @staticmethod
    def _cull_distant_monster_groups(
            s: Session,
            wire_position: tuple[float, float, float]) -> tuple[int, ...]:
        """Discard actors beyond the visibility exit hysteresis boundary."""

        chase_timer = None
        with s.flow_lock:
            encounter = s.gameplay_monster_encounter
            combat = s.gameplay_combat_state
            if encounter is None or combat is None:
                return ()
            world_position = encounter.coordinate_frame.wire_to_world(
                wire_position
            )
            spawned = set(s.gameplay_spawned_monster_actor_ids)
            announced = set(s.gameplay_announced_monster_actor_ids)
            standalone = s.gameplay_presence_callback is None
            fractured_run = bool(
                getattr(s, "gameplay_fractured_planes_run", False)
            )

        # Session visibility and map-world transforms are separate ownership
        # domains. Never wait for the shared map lock while monopolising the
        # session flow lock: projectile resolution takes the map lock first
        # and commits its timer state afterward, which otherwise forms AB/BA.
        with combat.lock:
            current_positions = dict(combat.monster_positions)
            persistent_scripted = {
                actor_id
                for actor_id in announced
                if (
                    combat.monsters.get(actor_id) is not None
                    and (
                        combat.monsters[actor_id].template_id.casefold()
                        in _PERSISTENT_SCRIPTED_ACTORS
                        or CharacterService._is_tower_hardware_actor(
                            encounter, combat, actor_id,
                        )
                        or (
                            fractured_run
                            and combat.monsters[actor_id].rank_id.casefold()
                            == "rank_boss_endlesstower"
                        )
                    )
                )
            }
        forgotten = {
            actor_id
            for actor_id in announced
            if actor_id not in persistent_scripted
            for position in (current_positions.get(actor_id),)
            if (
                position is None
                or (
                    (float(position[0]) - float(world_position[0])) ** 2
                    + (float(position[2]) - float(world_position[2])) ** 2
                ) > CharacterService._monster_visibility_radius(
                    encounter, combat, actor_id, exiting=True
                ) ** 2 + 1e-9
            )
        }
        discarded = forgotten.intersection(spawned)
        if not forgotten:
            return ()
        discarded_actor_ids = tuple(sorted(discarded))
        # Visibility is per client, but health/transforms are map-world state.
        # Never reset health, statuses, or transforms merely because the last
        # viewer walked out of range.
        with combat.lock:
            if discarded_actor_ids and standalone:
                combat.deactivate_monsters(
                    discarded_actor_ids, reset=False
                )
            map_has_background_work = bool(
                combat.player_summons or combat.has_regenerating_players
            )

        with s.flow_lock:
            # A map transfer may have completed while the detached map
            # snapshot was inspected. Stale culling must not edit the new
            # map's registration sets or cancel its simulation owner.
            if (
                s.gameplay_combat_state is not combat
                or s.gameplay_monster_encounter is not encounter
            ):
                return ()
            s.gameplay_spawned_monster_actor_ids.difference_update(
                discarded
            )
            CharacterService._forget_published_monsters(s, discarded)
            for actor_id in forgotten:
                s.gameplay_delivered_monster_positions.pop(actor_id, None)
            s.gameplay_announced_monster_actor_ids.difference_update(
                forgotten
            )
            s.gameplay_requested_actor_ids.difference_update(forgotten)
            s.gameplay_monsters_spawned = bool(
                s.gameplay_spawned_monster_actor_ids
            )
            s.gameplay_vicinity_announced = bool(
                s.gameplay_announced_monster_actor_ids
            )
            if (
                not s.gameplay_monsters_spawned
                and not map_has_background_work
            ):
                chase_timer = s.gameplay_chase_timer
                s.gameplay_chase_timer = None
                s.gameplay_chase_last_at = None
        if chase_timer is not None:
            chase_timer.cancel()
        if discarded_actor_ids:
            packet = encode_discard_monster_batch(discarded_actor_ids)
            s.send_reliable_ordered(
                packet,
                track=True,
                label="monster-discard-outside-vicinity",
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] discarded distant monster actors "
                f"{discarded_actor_ids}; active radius="
                f"{MONSTER_VISIBILITY_EXIT_RADIUS:.1f}"
            )
        if (
            s.gameplay_presence_callback is not None
            # Cancelling the simulation owner must always offer the map to a
            # peer. These two conditions are not the same: monsters that were
            # announced but never spawned leave ``discarded_actor_ids`` empty
            # while the branch above still retires the worker, which left the
            # map with no owner and no re-election event.
            and (discarded_actor_ids or chase_timer is not None)
        ):
            s.gameplay_presence_callback(
                s, ("monster_visibility_changed",)
            )
        return discarded_actor_ids

    @staticmethod
    def _handle_gameplay_move(s: Session, payload: bytes) -> None:
        move = decode_move(payload)
        if CharacterService._player_victim_feature_locked(s, "movement"):
            # The local client predicts movement. Silently dropping Move
            # leaves it walking while the server holds its old position. A
            # revive animation can produce many input packets in a row, so
            # correct to the frozen transform once per revive window instead
            # of repeatedly snapping the client back on every packet.
            with s.flow_lock:
                previous_move = s.gameplay_last_move
                frame = s.gameplay_coordinate_frame
                if frame is None and s.gameplay_combat_state is not None:
                    frame = s.gameplay_combat_state.encounter.coordinate_frame
                if previous_move is None and frame is not None and s.gameplay_spawn_position is not None:
                    previous_move = Move.from_values(
                        frame.world_to_wire(s.gameplay_spawn_position),
                        0.0, 0.0, 0.0, move.start_tick, 0,
                    )
                # Keep learning the client clock even while its position is
                # locked, so subsequent round effects use a current tick.
                if ((int(move.start_tick) - int(s.gameplay_chase_server_tick)) & 0xFFFFFFFF) < 0x80000000:
                    s.gameplay_chase_server_tick = move.start_tick
                revive_lock_until = float(
                    getattr(s, "gameplay_revive_movement_until", 0.0)
                )
                if revive_lock_until > 0.0:
                    correction_needed = (
                        float(getattr(
                            s,
                            "gameplay_revive_movement_correction_until",
                            0.0,
                        )) != revive_lock_until
                    )
                    if correction_needed:
                        s.gameplay_revive_movement_correction_until = (
                            revive_lock_until
                        )
                else:
                    correction_needed = True
                stopped = (
                    replace(previous_move, scalar_byte=0, duration_ticks=0,
                            start_tick=move.start_tick)
                    if previous_move is not None and correction_needed else None
                )
                if stopped is not None:
                    s.gameplay_last_move = stopped
            CharacterService._accept_gameplay_state_command(s, 0x005F)
            if stopped is not None:
                s.send_unreliable_sequenced(
                    encode_move(stopped, actor_id=LOCAL_PLAYER_ACTOR_ID)
                )
                if frame is not None:
                    s.send_reliable_ordered(
                        encode_force_transform_batch(ForceTransform(
                            frame.wire_to_world(stopped.relative_position), stopped.yaw,
                        ), actor_id=LOCAL_PLAYER_ACTOR_ID),
                        track=True, label="player-locked-movement-correction",
                    )
            return
        correction = None
        with s.flow_lock:
            s.gameplay_revive_movement_correction_until = 0.0
            previous_move = s.gameplay_last_move
            previous_move_received_at = float(
                getattr(s, "gameplay_last_move_received_at", 0.0) or 0.0
            )
            refusal = movement_validation.check_move(s, move)
            if refusal is None:
                s.gameplay_last_move = move
                accepted_move_at = server_time() / 1000.0
                s.gameplay_last_move_received_at = accepted_move_at
            else:
                # Retain the last valid server position and periodically send
                # a correction while the client continues to predict locally.
                now = time.perf_counter()
                frame = s.gameplay_coordinate_frame
                if frame is None and s.gameplay_combat_state is not None:
                    frame = s.gameplay_combat_state.encounter.coordinate_frame
                kept = s.gameplay_last_move
                if (
                    kept is None
                    and frame is not None
                    and s.gameplay_spawn_position is not None
                ):
                    kept = Move.from_values(
                        frame.world_to_wire(s.gameplay_spawn_position),
                        0.0, 0.0, 0.0, move.start_tick, 0,
                    )
                    s.gameplay_last_move = kept
                if (
                    kept is not None
                    and frame is not None
                    and now - s.gameplay_move_correction_at
                    >= movement_validation.CORRECTION_INTERVAL
                ):
                    s.gameplay_move_correction_at = now
                    correction = (
                        replace(
                            kept,
                            scalar_byte=0,
                            duration_ticks=0,
                            start_tick=move.start_tick,
                        ),
                        frame,
                    )
            if ((int(move.start_tick) - int(s.gameplay_chase_server_tick)) & 0xFFFFFFFF) < 0x80000000:
                s.gameplay_chase_server_tick = move.start_tick
        if refusal is not None:
            CharacterService._accept_gameplay_state_command(s, 0x005F)
            if correction is not None:
                stopped, frame = correction
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] refused move: {refusal}; "
                    f"corrected to {stopped.relative_position}"
                )
                s.send_unreliable_sequenced(
                    encode_move(stopped, actor_id=LOCAL_PLAYER_ACTOR_ID)
                )
                s.send_reliable_ordered(
                    encode_force_transform_batch(
                        ForceTransform(
                            frame.wire_to_world(stopped.relative_position),
                            stopped.yaw,
                        ),
                        actor_id=LOCAL_PLAYER_ACTOR_ID,
                    ),
                    track=True,
                    label="player-invalid-movement-correction",
                )
            return
        CharacterService._accept_gameplay_state_command(s, 0x005F)
        update = encode_move(move, actor_id=LOCAL_PLAYER_ACTOR_ID)
        s.send_unreliable_sequenced(update)
        # Resolve player-contact hazards as soon as the validated position is
        # accepted. Vicinity discovery and monster culling can do substantial
        # work on this movement path, so running collision after those steps
        # made a fast crossing appear to damage the player seconds later.
        CharacterService._check_trigger_volumes(
            s,
            move.relative_position,
            previous_wire_position=(
                previous_move.relative_position
                if previous_move is not None
                else None
            ),
            previous_wire_position_at=(
                previous_move_received_at or accepted_move_at
            ),
        )
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, 'move')
        if VERBOSE_PACKET_LOGGING:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] applied MoveCommand: "
                f"relative={move.relative_position}, ticks="
                f"{move.start_tick}+{move.duration_ticks}; sent "
                "authoritative 0x005f update"
            )
        if move.scalar_byte != 0:
            CharacterService._cancel_mount_cast(s, 'movement interrupted cast')
        CharacterService._cull_distant_monster_groups(
            s, move.relative_position
        )
        nearby_groups = ()
        proactive_actor_ids = set()
        with s.flow_lock:
            encounter = s.gameplay_monster_encounter
            combat = s.gameplay_combat_state
            announced_actor_ids = set(
                s.gameplay_announced_monster_actor_ids
            )
            spawned_actor_ids = set(
                s.gameplay_spawned_monster_actor_ids
            )
        if encounter is not None and combat is not None:
            with combat.lock:
                alive_actor_ids = set(combat.alive_actor_ids)
                current_positions = dict(combat.monster_positions)
                proactive_actor_ids = {
                    actor_id
                    for actor_id in alive_actor_ids
                    if (
                        combat.monsters.get(actor_id) is not None
                        and (
                            (
                                encounter.map_id.casefold()
                                == _FORTRESS_GRIMMAG_MAP
                                and combat.monsters[actor_id].template_id.casefold()
                                == _FORTRESS_GRIMMAG_TEMPLATE
                            )
                            or (
                                encounter.map_id.casefold() == _SMALL_CRYPT_MAP
                                and combat.monsters[actor_id].template_id.casefold()
                                == _SMALL_CRYPT_CAPTAIN_TEMPLATE
                            )
                            or (
                                encounter.map_id.casefold() == _TORSROND_MAP
                                and combat.monsters[actor_id].template_id.casefold()
                                == _TORSROND_CAPTAIN_TEMPLATE
                            )
                            or (
                                encounter.map_id.casefold()
                                == _WILDFOREST_CRYPT_EXIT_MAP
                                and combat.monsters[actor_id].template_id.casefold()
                                == _WILDFOREST_NEFERTARI_GUARD_TEMPLATE
                            )
                            or (
                                encounter.map_id.casefold() == _WILDHERZ_CAVERNS_MAP
                                and combat.monsters[actor_id].template_id.casefold()
                                == _WILDHERZ_YACHAK_TEMPLATE
                            )
                            or (
                                encounter.map_id.casefold() == _TEGAN_GRAVE_MAP
                                and combat.monsters[actor_id].template_id.casefold()
                                == _TEGAN_GRAVE_SMITH_TEMPLATE
                            )
                            or (
                                encounter.map_id.casefold() == _TIME_HARP_MAP
                                and combat.monsters[actor_id].template_id.casefold()
                                in _TIME_HARP_QUEST_MOB_TEMPLATES
                            )
                        )
                    )
                }
            player_world = encounter.coordinate_frame.wire_to_world(
                move.relative_position
            )
            nearby_groups = CharacterService._monster_groups_within_radius(
                encounter,
                world_position=player_world,
                actor_ids=(
                    (alive_actor_ids - announced_actor_ids
                     - CharacterService._pending_entrance_actor_ids(s, combat))
                    | (proactive_actor_ids - spawned_actor_ids)
                ),
                positions=current_positions,
                radius=MONSTER_VISIBILITY_ENTER_RADIUS,
                extra_groups=combat.runtime_groups,
                radius_for_actor=lambda actor_id: (
                    CharacterService._monster_visibility_radius(
                        encounter, combat, actor_id
                    )
                ),
            )
        with s.flow_lock:
            if (
                s.gameplay_combat_state is not combat
                or s.gameplay_monster_encounter is not encounter
            ):
                nearby_groups = ()
                proactive_spawn_ids = set()
            else:
                # These authored quest and champion actors can be missing if
                # the client does not request them after vicinity registration.
                # Publish them before vicinity and reserve their IDs so late
                # ActorRequests cannot create duplicates.
                proactive_spawn_ids = {
                    actor_id
                    for group in nearby_groups
                    for actor_id in group.actor_ids
                    if actor_id in proactive_actor_ids
                    and actor_id not in s.gameplay_spawned_monster_actor_ids
                }
                for group in nearby_groups:
                    s.gameplay_announced_monster_actor_ids.update(
                        group.actor_ids
                    )
                s.gameplay_spawned_monster_actor_ids.update(
                    proactive_spawn_ids
                )
                s.gameplay_requested_actor_ids.update(proactive_spawn_ids)
                if proactive_spawn_ids:
                    s.gameplay_monsters_spawned = True
                s.gameplay_vicinity_announced = bool(
                    s.gameplay_announced_monster_actor_ids
                )
        for group in nearby_groups:
            grimmag_ids = tuple(
                actor_id
                for actor_id in group.actor_ids
                if (
                    combat.monsters.get(actor_id) is not None
                    and combat.monsters[actor_id].template_id.casefold()
                    == _FORTRESS_GRIMMAG_TEMPLATE
                )
            )
            if grimmag_ids:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] announced fortress Grimmag "
                    f"actor(s) {grimmag_ids} to {s.addr}"
                )
            spawn_ids_in_group = proactive_spawn_ids.intersection(
                group.actor_id_set
            )
            if spawn_ids_in_group:
                scripted_group = group.runtime_subset(
                    spawn_ids_in_group,
                    positions=current_positions,
                )
                if scripted_group is not None:
                    batch = CharacterService._new_monster_batch_for_viewer(
                        s, combat, scripted_group.spawn_batch,
                        label="authored-priority-monster-spawn",
                    )
                else:
                    batch = None
                if batch is not None:
                    packet, packet_bits = encode_monster_spawn_batch(batch)
                    if len(packet) > TAIL_FRAG_SIZE:
                        s.send_reliable_ordered_split(
                            packet,
                            total_bit_length=packet_bits,
                            track=True,
                            label_prefix="authored-priority-monster-spawn",
                        )
                    else:
                        s.send_reliable_ordered(
                            (packet, packet_bits),
                            track=True,
                            label="authored-priority-monster-spawn",
                        )
                    delivered_positions = {
                        actor_id: current_positions[actor_id]
                        for actor_id in scripted_group.actor_ids
                        if actor_id in current_positions
                    }
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] proactively sent authored "
                        f"priority NewMonster actor(s) "
                        f"{scripted_group.actor_ids} at "
                        f"{tuple(delivered_positions.values())!r} to {s.addr}"
                    )
            # Register the selected champion before vicinity creates the
            # client's placeholder. Other monsters keep the ordinary
            # vicinity-then-ActorRequest flow.
            CharacterService._send_monster_vicinity(
                s, combat, group,
                label="monster-actors-enter-vicinity",
            )
            if spawn_ids_in_group and scripted_group is not None:
                with combat.lock:
                    combat.activate_monsters(scripted_group.actor_ids)
                with s.flow_lock:
                    if s.gameplay_combat_state is combat:
                        s.gameplay_delivered_monster_positions.update(
                            delivered_positions
                        )
            if VERBOSE_PACKET_LOGGING:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] announced nearby monster actors "
                    f"{group.actor_ids} inside "
                    f"{MONSTER_VISIBILITY_ENTER_RADIUS:.1f}-unit enter radius"
                )
        if nearby_groups and s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(
                s,
                (
                    "monster_groups_announced",
                    tuple(
                        actor_id
                        for group in nearby_groups
                        for actor_id in group.actor_ids
                    ),
                ),
            )
        CharacterService._check_map_companions(s, move.relative_position)
        CharacterService._reconcile_world_loot_visibility(
            s,
            move.relative_position,
        )
        CharacterService._pickup_nearby_auto_loot(
            s, move.relative_position
        )

    @staticmethod
    def _handle_actor_request(s: Session, payload: bytes) -> None:
        decoded = decode_simple_gameplay_command(payload)
        spawn_batch = None
        spawned_actor_ids = ()
        rejected_for_distance = False
        with s.flow_lock:
            s.gameplay_client_command_state[0x001C] = decoded
            encounter = s.gameplay_monster_encounter
            combat = s.gameplay_combat_state
            last_move = s.gameplay_last_move

        if combat is not None:
            with combat.lock:
                alive_actor_ids = set(combat.alive_actor_ids)
                current_positions = dict(combat.monster_positions)
        else:
            alive_actor_ids = (
                set(encounter.actor_ids)
                if encounter is not None
                else set()
            )
            current_positions = {}
        original_group = (
            combat.group_for_actor(decoded.value)
            if combat is not None
            else encounter.group_for_actor(decoded.value)
            if encounter is not None
            else None
        )
        if decoded.value in CharacterService._pending_entrance_actor_ids(s, combat):
            return
        requested_group = (
            original_group.runtime_subset(
                {int(decoded.value)},
                positions=current_positions or None,
            )
            if (
                original_group is not None
                and int(decoded.value) in alive_actor_ids
            )
            else None
        )
        actor_is_near = False
        player_world = None
        if encounter is not None:
            if last_move is not None:
                player_world = encounter.coordinate_frame.wire_to_world(
                    last_move.relative_position
                )
            elif s.gameplay_spawn_position is not None:
                player_world = tuple(float(v) for v in s.gameplay_spawn_position)
        if (
            requested_group is not None
            and decoded.value in requested_group.actor_ids
            and player_world is not None
        ):
            actor_position = current_positions.get(decoded.value)
            if actor_position is not None:
                actor_is_near = (
                    (float(actor_position[0]) - float(player_world[0])) ** 2
                    + (float(actor_position[2]) - float(player_world[2])) ** 2
                ) <= CharacterService._monster_visibility_radius(
                    encounter, combat, decoded.value, exiting=True
                ) ** 2 + 1e-9

        activate_actor_ids = ()
        with s.flow_lock:
            if (
                s.gameplay_combat_state is not combat
                or s.gameplay_monster_encounter is not encounter
            ):
                requested_group = None
                rejected_for_distance = True
            elif (
                original_group is not None
                and decoded.value not in alive_actor_ids
            ):
                # Dead map-world actors remain dead until the encounter is
                # explicitly reset. A late client ActorRequest must not
                # recreate their visual object.
                s.gameplay_announced_monster_actor_ids.discard(
                    decoded.value
                )
                s.gameplay_requested_actor_ids.discard(decoded.value)
                rejected_for_distance = True
            elif (
                requested_group is not None
                and decoded.value
                in s.gameplay_announced_monster_actor_ids
                and not actor_is_near
            ):
                # Requests are serial and may arrive after the player crossed
                # the enter boundary. Honor the wider exit boundary, but
                # reject only this actor if it has genuinely left.
                s.gameplay_announced_monster_actor_ids.discard(decoded.value)
                s.gameplay_requested_actor_ids.discard(decoded.value)
                s.gameplay_vicinity_announced = bool(
                    s.gameplay_announced_monster_actor_ids
                )
                rejected_for_distance = True
            if (
                requested_group is not None
                and actor_is_near
                and decoded.value in s.gameplay_announced_monster_actor_ids
            ):
                s.gameplay_requested_actor_ids.add(decoded.value)
                if (
                    decoded.value
                    not in s.gameplay_spawned_monster_actor_ids
                ):
                    spawn_batch = requested_group.spawn_batch
                    spawned_actor_ids = requested_group.actor_ids
                    activate_actor_ids = requested_group.actor_ids
                    # Not visible yet: map workers read this set, and must not
                    # send Move/attacks before NewMonster has its ordered
                    # slot (below). A repeated or concurrent ActorRequest is
                    # deduplicated by _new_monster_batch_for_viewer's ledger.
        CharacterService._accept_gameplay_state_command(s, 0x001C)
        if (
            original_group is None
            and s.gameplay_presence_callback is not None
        ):
            s.gameplay_presence_callback(
                s, ("actor_request", decoded.value)
            )
        if rejected_for_distance:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] ignored late ActorRequest for "
                f"{decoded.value}: actor left "
                f"{MONSTER_VISIBILITY_EXIT_RADIUS:.1f}-unit exit radius"
            )
        if spawn_batch is not None:
            spawn_batch = CharacterService._new_monster_batch_for_viewer(
                s, combat, spawn_batch, label="monster-spawn-batch",
            )
        if spawn_batch is not None:
            packet, packet_bits = encode_monster_spawn_batch(spawn_batch)
            if len(packet) > TAIL_FRAG_SIZE:
                s.send_reliable_ordered_split(
                    packet,
                    total_bit_length=packet_bits,
                    track=True,
                    label_prefix=(
                        'monster-spawn-batch-monster-spawn-batch'
                    ),
                )
            else:
                s.send_reliable_ordered(
                    (packet, packet_bits),
                    track=True,
                    label="monster-spawn-batch",
                )
            # Keep the visibility publication here, after NewMonster has an
            # ordered slot, so map workers cannot send Move/attacks ahead of
            # actor creation.
            with s.flow_lock:
                if s.gameplay_combat_state is not combat:
                    return
                s.gameplay_spawned_monster_actor_ids.update(spawned_actor_ids)
                s.gameplay_monsters_spawned = True
            if combat is not None and activate_actor_ids:
                with combat.lock:
                    combat.activate_monsters(activate_actor_ids)
                # An ActorRequest can race a lethal hit: it snapshots an
                # actor while alive, then queues NewMonster after the Kill
                # transaction. The client would recreate the dead actor from
                # that stale snapshot and leave its corpse visible. Recheck
                # after NewMonster is ordered and retire any such actor after
                # it, so the reliable stream always ends in the dead state.
                with combat.lock:
                    dead_after_spawn = tuple(
                        actor_id
                        for actor_id in activate_actor_ids
                        if combat.monster_health.get(actor_id, 0) <= 0
                    )
                if dead_after_spawn:
                    with s.flow_lock:
                        if s.gameplay_combat_state is not combat:
                            dead_after_spawn = ()
                        else:
                            s.gameplay_spawned_monster_actor_ids.difference_update(
                                dead_after_spawn
                            )
                            CharacterService._forget_published_monsters(
                                s, dead_after_spawn
                            )
                            s.gameplay_announced_monster_actor_ids.difference_update(
                                dead_after_spawn
                            )
                            s.gameplay_requested_actor_ids.difference_update(
                                dead_after_spawn
                            )
                            s.gameplay_delivered_monster_positions = {
                                actor_id: position
                                for actor_id, position
                                in s.gameplay_delivered_monster_positions.items()
                                if actor_id not in dead_after_spawn
                            }
                            s.gameplay_monsters_spawned = bool(
                                s.gameplay_spawned_monster_actor_ids
                            )
                            s.gameplay_vicinity_announced = bool(
                                s.gameplay_announced_monster_actor_ids
                            )
                    if dead_after_spawn:
                        s.send_reliable_ordered(
                            encode_discard_monster_batch(dead_after_spawn),
                            track=True,
                            label="monster-spawn-race-discard",
                        )
                        print(
                            f"[{GAMEPLAY_SERVICE_NAME}] discarded dead actor(s) "
                            f"recreated by a late ActorRequest: {dead_after_spawn}"
                        )
                        spawned_actor_ids = tuple(
                            actor_id
                            for actor_id in spawned_actor_ids
                            if actor_id not in dead_after_spawn
                        )
            if VERBOSE_PACKET_LOGGING:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] fulfilled ActorRequest for "
                    f"{spawned_actor_ids}; sent ordered NewMonster state with "
                    "non-empty permanent effects"
                )
            with s.flow_lock:
                combat = s.gameplay_combat_state
                server_tick = s.gameplay_chase_server_tick
            if combat is not None:
                with combat.lock:
                    delivered_positions = {
                        actor_id: combat.monster_positions[actor_id]
                        for actor_id in spawned_actor_ids
                        if actor_id in combat.monster_positions
                    }
                with s.flow_lock:
                    if s.gameplay_combat_state is combat:
                        s.gameplay_delivered_monster_positions.update(
                            delivered_positions
                        )
            if (
                combat is not None
                and s.gameplay_presence_callback is not None
            ):
                snapshot_movements = tuple(
                    SimpleNamespace(
                        actor_id=actor_id,
                        wire_position=(
                            combat.encounter.coordinate_frame.world_to_wire(
                                combat.monster_positions[actor_id]
                            )
                        ),
                        yaw=combat.monster_yaws[actor_id],
                        moving=combat.monster_moving[actor_id],
                        server_tick=server_tick,
                    )
                    for actor_id in spawned_actor_ids
                )
                CharacterService._send_monster_activity_to(
                    s,
                    combat,
                    snapshot_movements,
                    (),
                    (),
                    (),
                )
            if combat is not None:
                with combat.lock:
                    returning = tuple(actor_id for actor_id in spawned_actor_ids
                                      if actor_id in combat.returning_home_actor_ids)
                    effect_id, duration = combat._return_home_effect() if returning else (0, 0)
                for actor_id in returning:
                    CharacterService._send_monster_return_home_transition(s, SimpleNamespace(
                        actor_id=actor_id, active=True, effect_id=effect_id,
                        instance_id=combat._return_home_instance_id(actor_id),
                        start_tick=server_tick, duration_ticks=duration,
                    ))
            CharacterService._start_monster_chase(s)
        requested_monster = (
            combat.monsters.get(int(decoded.value))
            if combat is not None
            else None
        )
        if (
            requested_monster is not None
            and encounter is not None
            and encounter.map_id.casefold() == _FORTRESS_GRIMMAG_MAP
            and requested_monster.template_id.casefold()
            == _FORTRESS_GRIMMAG_TEMPLATE
        ):
            with s.flow_lock:
                announced = int(decoded.value) in s.gameplay_announced_monster_actor_ids
                spawned = int(decoded.value) in s.gameplay_spawned_monster_actor_ids
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] fortress Grimmag ActorRequest "
                f"actor={decoded.value}, near={actor_is_near}, "
                f"announced={announced}, spawned={spawned}, "
                f"rejected={rejected_for_distance}, "
                f"response={'NewMonster' if spawn_batch is not None else 'none'} "
                f"to {s.addr}"
            )

    @staticmethod
    def _send_world_loot_batch(s: Session, drops) -> None:
        packet, packet_bits = encode_new_item_batch(drops)
        if len(packet) > TAIL_FRAG_SIZE:
            s.send_reliable_ordered_split(
                packet,
                total_bit_length=packet_bits,
                track=True,
                label_prefix='world-loot-spawn-world-loot-spawn',
            )
        else:
            s.send_reliable_ordered(
                (packet, packet_bits),
                track=True,
                label="world-loot-spawn",
            )

    @staticmethod
    def _send_world_loot_state_batch(
            s: Session, packet: bytes, packet_bits: int, *, label: str) -> None:
        if len(packet) > TAIL_FRAG_SIZE:
            s.send_reliable_ordered_split(
                packet,
                total_bit_length=packet_bits,
                track=True,
                label_prefix=f'{label}-{label}',
            )
        else:
            s.send_reliable_ordered(
                (packet, packet_bits),
                track=True,
                label=label,
            )

    @staticmethod
    def _reconcile_world_loot_visibility(
            s: Session,
            wire_position: tuple[float, float, float] | None = None,
            *,
            label: str = "world-loot-vicinity",
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """Publish the exact world-item subset visible to one client.

        ``gameplay_world_loot`` is authoritative map state.  The announced
        set is client registration state; sending NewItem twice without an
        intervening DiscardItem triggers GameWorld::RegisterItem and
        ItemManager assertions in the installed client.
        """

        # Transport reliability bookkeeping has its own lock. Snapshot it
        # before entering gameplay state so this path cannot contribute a
        # flow -> pending edge to a future lock cycle.
        backlog = s.reliable_backlog_size()
        with s.flow_lock:
            encounter = s.gameplay_monster_encounter
            if wire_position is None and s.gameplay_last_move is not None:
                wire_position = s.gameplay_last_move.relative_position
            if encounter is None or wire_position is None:
                visible_ids = set(s.gameplay_world_loot)
            else:
                player_world = encounter.coordinate_frame.wire_to_world(
                    wire_position
                )
                visible_ids = {
                    int(item_id)
                    for item_id, drop in s.gameplay_world_loot.items()
                    if math.hypot(
                        float(drop.item.field_100[0]) - player_world[0],
                        float(drop.item.field_100[2]) - player_world[2],
                    ) <= WORLD_LOOT_VICINITY_RADIUS
                }
            announced = s.gameplay_announced_world_item_ids
            leaving = tuple(sorted(announced - visible_ids))
            entering_ids = (
                ()
                if backlog >= RELIABLE_BACKLOG_SOFT_LIMIT
                else tuple(sorted(visible_ids - announced))
            )
            entering = tuple(
                s.gameplay_world_loot[item_id]
                for item_id in entering_ids
                if item_id in s.gameplay_world_loot
            )
            # Update before writing to the socket. Concurrent movement and
            # pickup callbacks must observe the intended registration state,
            # not race a second NewItem/DiscardItem onto the ordered stream.
            announced.difference_update(leaving)
            announced.update(
                drop.item.item_id for drop in entering
            )

        if leaving:
            packet, packet_bits = encode_discard_item_batch(leaving)
            CharacterService._send_world_loot_state_batch(
                s,
                packet,
                packet_bits,
                label=f"{label}-discard",
            )
        if entering:
            CharacterService._send_world_loot_batch(s, entering)
        if (leaving or entering) and VERBOSE_PACKET_LOGGING:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] reconciled world loot for "
                f"{s.addr}: discarded={leaving}, entered="
                f"{tuple(drop.item.item_id for drop in entering)}"
            )
        return leaving, tuple(drop.item.item_id for drop in entering)

    @staticmethod
    def _pickup_world_item(
            s: Session, item_id: int, *, automatic: bool) -> bool:
        '''Serialize one session's world-item claim through final removal.'''

        with s.flow_lock:
            pickup_lock = getattr(
                s, 'gameplay_world_loot_pickup_lock', None
            )
            if pickup_lock is None:
                pickup_lock = threading.RLock()
                s.gameplay_world_loot_pickup_lock = pickup_lock
        with s.flow_lock:
            drop = s.gameplay_world_loot.get(int(item_id))
            template_id = str(getattr(getattr(drop, "item", None), "template_id", ""))
        with pickup_lock:
            picked = CharacterService._pickup_world_item_serialized(
                s, item_id, automatic=automatic
            )
        if picked and template_id:
            # Static code has no server handle; quests listen through the
            # presence callback like kills do (services/presence.py).
            callback = getattr(s, "gameplay_presence_callback", None)
            if callback is not None:
                try:
                    event = ("item_collected", template_id, max(1, int(drop.item.amount)))
                    if drop.quest_id:
                        event += (drop.quest_id, drop.quest_collect_id or template_id)
                    callback(s, event)
                except Exception as exc:
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] quest collect notification "
                        f"failed: {type(exc).__name__}: {exc}"
                    )
        return picked

    @staticmethod
    def _pickup_world_item_serialized(
            s: Session, item_id: int, *, automatic: bool) -> bool:
        """Consume one registered world item and publish its destination."""

        heal_transition = None
        healed = None
        healed_actor_id = None
        live_health = None
        with s.flow_lock:
            candidate_drop = s.gameplay_world_loot.get(int(item_id))
            candidate_combat = s.gameplay_combat_state
            candidate_inventory = s.gameplay_inventory
            candidate_generation = s.gameplay_chase_generation
            character = s.gameplay_character
            presence_enabled = s.gameplay_presence_callback is not None
            remote_actor_id = s.gameplay_remote_actor_id
            event_catalog = s.gameplay_event_catalog
        if candidate_drop is None:
            return False
        event_collectible = bool(
            event_catalog is not None
            and event_catalog.is_event_collectible(
                candidate_drop.item.template_id
            )
        )
        instant_database_path = getattr(
            getattr(candidate_combat, 'encounter', None),
            'static_database_path',
            DEFAULT_STATIC_DB_PATH,
        )
        instant_definition = (
            load_instant_consumable_definition(
                instant_database_path,
                candidate_drop.item.template_id,
            )
            if candidate_drop.item_category == 'InstantConsumable'
            else None
        )
        health_globe = bool(
            candidate_drop.item.template_id.startswith('healthglobe_')
            or (
                instant_definition is not None
                and instant_definition.effect_resource_id.casefold()
                == 'healthglobe'
            )
        )

        # Combat owns live health. Mutate it before the inventory transaction;
        # session flow must never wait on the shared map lock.
        if (
            candidate_drop.item_category == 'InstantConsumable'
            and not event_collectible
            and health_globe
            and candidate_combat is not None
        ):
            player_actor_id = candidate_combat.player_actor_id
            if presence_enabled and character is not None:
                player_actor_id = (
                    remote_actor_id
                    or CharacterService._remote_actor_id(character.character_id)
                )
            healed_actor_id = int(player_actor_id)
            with candidate_combat.lock:
                previous = candidate_combat.player_health.get(
                    int(player_actor_id),
                    [candidate_combat.player_maximum_health],
                )[0]
                if int(previous) <= 0:
                    return False
                current, maximum = candidate_combat.heal_player(
                    full=candidate_drop.item.template_id.endswith('_full'),
                    actor_id=player_actor_id,
                )
            healed = (int(previous), int(current), int(maximum))
        elif (
            candidate_drop.item.template_id != WISDOM_ITEM_TEMPLATE
            and candidate_drop.item_category not in {
                'Currency', 'InstantConsumable'
            }
            and candidate_combat is not None
            and candidate_inventory is not None
        ):
            player_actor_id = candidate_combat.player_actor_id
            if presence_enabled and character is not None:
                player_actor_id = (
                    remote_actor_id
                    or CharacterService._remote_actor_id(character.character_id)
                )
            with candidate_combat.lock:
                health = candidate_combat.player_health.get(
                    int(player_actor_id)
                )
                if health is not None:
                    live_health = max(0, int(health[0]))

        with (
            _rollback_uncommitted_world_pickup_heal(
                candidate_combat, healed_actor_id, healed
            ) as heal_commit,
            s.flow_lock,
        ):
            drop = s.gameplay_world_loot.get(int(item_id))
            if (
                drop is not candidate_drop
                or s.gameplay_combat_state is not candidate_combat
                or s.gameplay_chase_generation != candidate_generation
            ):
                return False
            inventory = s.gameplay_inventory
            currency = s.gameplay_currency_state
            combat = s.gameplay_combat_state
            wisdom_pickup = (
                drop.item.template_id == WISDOM_ITEM_TEMPLATE
            )
            try:
                if wisdom_pickup:
                    points = tuple(s.gameplay_talent_category_points)
                    maximum = max(0, int(s.gameplay_wisdom_maximum_xp))
                    if len(points) != 4 or maximum <= 0:
                        raise ValueError(
                            "wisdom progression state is unavailable"
                        )
                    updated_wisdom = min(
                        maximum,
                        int(points[1]) + max(1, int(drop.item.amount)),
                    )
                    s.gameplay_talent_category_points = (
                        int(points[0]),
                        updated_wisdom,
                        int(points[2]),
                        int(points[3]),
                    )
                    response_packet, response_bits = (
                        encode_wisdom_pickup_response(
                            drop, actor_id=LOCAL_PLAYER_ACTOR_ID
                        )
                    )
                elif drop.item_category == 'Currency':
                    if currency is None:
                        raise ValueError('player currency state is unavailable')
                    currency = credit_currency_drop(currency, drop)
                    response_packet, response_bits = (
                        encode_world_pickup_response(
                            drop,
                            actor_id=LOCAL_PLAYER_ACTOR_ID,
                            currency=currency,
                        )
                    )
                    s.gameplay_currency_state = currency
                elif drop.item_category == 'InstantConsumable':
                    if healed is not None:
                        previous, current, maximum = healed
                        recovered = max(0, int(current) - int(previous))
                        if recovered > 0:
                            next_tick = max(
                                int(s.gameplay_chase_server_tick),
                                int(s.gameplay_last_heal_server_tick) + 1,
                            ) & 0xFFFFFFFF
                            s.gameplay_last_heal_server_tick = next_tick
                            heal_transition = (
                                int(current), int(maximum), recovered, next_tick
                            )
                    response_packet, response_bits = (
                        encode_world_pickup_response(
                            drop,
                            actor_id=LOCAL_PLAYER_ACTOR_ID,
                        )
                    )
                    if healed is not None and inventory is not None:
                        inventory.health_points = float(current)
                else:
                    if inventory is None:
                        raise ValueError('player inventory is unavailable')
                    if live_health is not None:
                        inventory.health_points = live_health
                    item_definition = load_purchase_item_definition(
                        instant_database_path, drop.item.template_id,
                    )
                    mutation = add_world_drop_to_inventory(
                        inventory, drop,
                        max_stack_size=item_definition.max_stack_size,
                        next_item_id=int(s.gameplay_next_item_id),
                        reserved_item_ids=tuple(s.gameplay_world_loot),
                    )
                    response_packet, response_bits = (
                        encode_world_pickup_response(
                            drop,
                            actor_id=LOCAL_PLAYER_ACTOR_ID,
                            inventory=mutation,
                        )
                    )
                    s.gameplay_inventory = mutation.inventory
                    if mutation.inventory.items:
                        s.gameplay_next_item_id = max(
                            int(s.gameplay_next_item_id),
                            max(int(item.item_id) for item in mutation.inventory.items) + 1,
                        )
                del s.gameplay_world_loot[int(item_id)]
                # The pickup response contains the local DiscardItem. Remove
                # the registration before peer callbacks can reconcile it.
                s.gameplay_announced_world_item_ids.discard(int(item_id))
                heal_commit[0] = True
            except ValueError as exc:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] rejected world-loot pickup "
                    f"{item_id}: {exc}"
                )
                return False

        CharacterService._send_world_loot_state_batch(
            s,
            response_packet,
            response_bits,
            label='world-loot-pickup-response',
        )
        if wisdom_pickup:
            pickup_packet, pickup_bits = encode_pickup_info_batch(
                drop.item.template_id
            )
            CharacterService._send_world_loot_state_batch(
                s,
                pickup_packet,
                pickup_bits,
                label='world-loot-pickup-info',
            )
        if instant_definition is not None:
            CharacterService._apply_instant_consumable_effect(
                s,
                instant_definition,
                database_path=instant_database_path,
                apply_health=not health_globe,
            )
        event_progressed = False
        if event_collectible:
            with s.flow_lock:
                progress_callback = s.gameplay_event_progress_callback
            if progress_callback is not None:
                event_progressed = bool(progress_callback(
                    s,
                    drop.item.template_id,
                    max(1, int(drop.item.amount)),
                ))
        if heal_transition is not None:
            current, maximum, recovered, server_tick = heal_transition
            CharacterService._send_player_heal_transition(
                s,
                current_health=current,
                maximum_health=maximum,
                recovered=recovered,
                server_tick=server_tick,
                label="health-sphere-heal",
            )
            CharacterService._dispatch_socket_event(
                s,
                "pickuphealthglobe",
                server_tick=server_tick,
                event_seed=int(item_id),
            )
        if wisdom_pickup:
            with s.flow_lock:
                points = tuple(s.gameplay_talent_category_points)
                talent_categories = copy.deepcopy(
                    s.gameplay_talent_categories
                )
            progress_packet, progress_bits = encode_server_command_batch(
                (
                    ServerCommand(
                        TALENT_CATEGORY_POINTS_INFO_COMMAND,
                        # Match the complete category-point snapshot sent on
                        # map initialization. The client does not refresh its
                        # live Knowledge counter from a category-only update.
                        TalentCategoryPointsInfo(0xFF, points),
                        0,
                    ),
                    # The progression value is authoritative immediately,
                    # but the open Wisdom tree caches its own available-point
                    # summary. Repeating the complete selection snapshot in
                    # the same ordered batch makes it recalculate from the
                    # new XP instead of waiting for a map change/relogin.
                    ServerCommand(
                        TALENT_SELECTION_INFO_COMMAND,
                        TalentSelectionInfo(0, tuple(talent_categories)),
                        0,
                    ),
                ),
                include_terminator=True,
            )
            CharacterService._send_world_loot_state_batch(
                s,
                progress_packet,
                progress_bits,
                label="wisdom-progression-update",
            )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] {'auto-picked' if automatic else 'picked'} "
            f"world item {item_id} ({drop.item.template_id} x{drop.item.amount})"
            + (
                f"; wisdom={points[1]}/{s.gameplay_wisdom_maximum_xp}"
                if wisdom_pickup
                else ""
            )
        )
        if (
            (not automatic or wisdom_pickup or event_collectible)
            and not event_progressed
        ):
            CharacterService._request_account_save(
                s, 'world loot pickup'
            )
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(
                s, ("world_loot_picked", int(item_id))
            )
        return True

    @staticmethod
    def _pickup_nearby_auto_loot(
            s: Session, wire_position: tuple[float, float, float]) -> None:
        with s.flow_lock:
            encounter = s.gameplay_monster_encounter
            if encounter is None:
                return
            player_position = encounter.coordinate_frame.wire_to_world(
                wire_position
            )
            item_ids = tuple(
                item_id
                for item_id, drop in s.gameplay_world_loot.items()
                if (
                    drop.auto_pick_up == 'All'
                    and drop.item_category in {
                        'Currency', 'InstantConsumable'
                    }
                    and math.hypot(
                        drop.item.field_100[0] - player_position[0],
                        drop.item.field_100[2] - player_position[2],
                    ) <= 2.5
                )
            )
        for item_id in item_ids:
            CharacterService._pickup_world_item(
                s, item_id, automatic=True
            )

    @staticmethod
    def _handle_pickup_item(s: Session, payload: bytes) -> None:
        item_id = decode_pickup_item(payload)
        with s.flow_lock:
            drop = s.gameplay_world_loot.get(item_id)
            encounter = s.gameplay_monster_encounter
            last_move = s.gameplay_last_move
            if drop is None:
                distance = None
            elif encounter is None or last_move is None:
                distance = 0.0
            else:
                player_position = encounter.coordinate_frame.wire_to_world(
                    last_move.relative_position
                )
                distance = math.hypot(
                    drop.item.field_100[0] - player_position[0],
                    drop.item.field_100[2] - player_position[2],
                )
        CharacterService._accept_gameplay_state_command(s, PICKUP_ITEM_COMMAND)
        if drop is None:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected PickupItem {item_id}: "
                "unknown world item"
            )
            return
        if distance is not None and distance > 4.0:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected PickupItem {item_id}: "
                f"distance {distance:.2f} exceeds pickup range"
            )
            return
        CharacterService._pickup_world_item(s, item_id, automatic=False)
