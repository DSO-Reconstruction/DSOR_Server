"""Movement, actor discovery, world loot, and vicinity state."""

from __future__ import annotations

from contextlib import contextmanager
import threading

from dso_server.protocol.consumable_protocol import load_instant_consumable_definition

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


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
    def _monster_groups_within_radius(
            encounter, *, world_position, actor_ids, positions,
            radius: float, extra_groups=()):
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
        radius_squared = max(0.0, float(radius)) ** 2
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
                    ) <= radius_squared + 1e-9
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

        # Session visibility and map-world transforms are separate ownership
        # domains. Never wait for the shared map lock while monopolising the
        # session flow lock: projectile resolution takes the map lock first
        # and commits its timer state afterward, which otherwise forms AB/BA.
        with combat.lock:
            current_positions = dict(combat.monster_positions)
        exit_radius_squared = MONSTER_VISIBILITY_EXIT_RADIUS ** 2
        forgotten = {
            actor_id
            for actor_id in announced
            for position in (current_positions.get(actor_id),)
            if (
                position is None
                or (
                    (float(position[0]) - float(world_position[0])) ** 2
                    + (float(position[2]) - float(world_position[2])) ** 2
                ) > exit_radius_squared + 1e-9
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
            # leaves it walking while the server holds its old position.
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
                stopped = (
                    replace(previous_move, scalar_byte=0, duration_ticks=0,
                            start_tick=move.start_tick)
                    if previous_move is not None else None
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
        with s.flow_lock:
            s.gameplay_last_move = move
            if ((int(move.start_tick) - int(s.gameplay_chase_server_tick)) & 0xFFFFFFFF) < 0x80000000:
                s.gameplay_chase_server_tick = move.start_tick
        CharacterService._accept_gameplay_state_command(s, 0x005F)
        update = encode_move(move, actor_id=LOCAL_PLAYER_ACTOR_ID)
        s.send_unreliable_sequenced(update)
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
        with s.flow_lock:
            encounter = s.gameplay_monster_encounter
            combat = s.gameplay_combat_state
            announced_actor_ids = set(
                s.gameplay_announced_monster_actor_ids
            )
        if encounter is not None and combat is not None:
            with combat.lock:
                alive_actor_ids = set(combat.alive_actor_ids)
                current_positions = dict(combat.monster_positions)
            player_world = encounter.coordinate_frame.wire_to_world(
                move.relative_position
            )
            nearby_groups = CharacterService._monster_groups_within_radius(
                encounter,
                world_position=player_world,
                actor_ids=alive_actor_ids - announced_actor_ids,
                positions=current_positions,
                radius=MONSTER_VISIBILITY_ENTER_RADIUS,
                extra_groups=combat.runtime_groups,
            )
        with s.flow_lock:
            if (
                s.gameplay_combat_state is not combat
                or s.gameplay_monster_encounter is not encounter
            ):
                nearby_groups = ()
            else:
                for group in nearby_groups:
                    s.gameplay_announced_monster_actor_ids.update(
                        group.actor_ids
                    )
                s.gameplay_vicinity_announced = bool(
                    s.gameplay_announced_monster_actor_ids
                )
        for group in nearby_groups:
            CharacterService._send_monster_vicinity(
                s, combat, group,
                label="monster-actors-enter-vicinity",
            )
            if VERBOSE_PACKET_LOGGING:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] announced nearby monster actors "
                    f"{group.actor_ids} inside "
                    f"{MONSTER_VISIBILITY_ENTER_RADIUS:.1f}-unit enter radius; "
                    "awaiting ActorRequest for each"
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
        CharacterService._check_trigger_volumes(s, move.relative_position)
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
        if (
            requested_group is not None
            and decoded.value in requested_group.actor_ids
            and last_move is not None
        ):
            player_world = encounter.coordinate_frame.wire_to_world(
                last_move.relative_position
            )
            actor_position = current_positions.get(decoded.value)
            if actor_position is not None:
                actor_is_near = (
                    (float(actor_position[0]) - float(player_world[0])) ** 2
                    + (float(actor_position[2]) - float(player_world[2])) ** 2
                ) <= MONSTER_VISIBILITY_EXIT_RADIUS ** 2 + 1e-9

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
            # Publish visibility only after NewMonster has an ordered slot.
            # Another viewer's map worker must not send this actor's Move or
            # attack ahead of its creation on the new connection.
            with s.flow_lock:
                if s.gameplay_combat_state is not combat:
                    return
                s.gameplay_spawned_monster_actor_ids.update(spawned_actor_ids)
                s.gameplay_monsters_spawned = True
            if combat is not None and activate_actor_ids:
                with combat.lock:
                    combat.activate_monsters(activate_actor_ids)
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
                        encode_discard_item_batch((int(item_id),))
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
                    mutation = add_world_drop_to_inventory(inventory, drop)
                    response_packet, response_bits = (
                        encode_world_pickup_response(
                            drop,
                            actor_id=LOCAL_PLAYER_ACTOR_ID,
                            inventory=mutation,
                        )
                    )
                    s.gameplay_inventory = mutation.inventory
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
                        # Incremental category refresh: 1 is Ancient Wisdom.
                        # 0xFF is reserved for the full initialization/reset
                        # snapshot and does not refresh the live wisdom UI.
                        # The category and the values are both in category
                        # order here; the codec permutes them into the wire's
                        # own order (see TALENT_CATEGORY_POINTS_WIRE_ORDER).
                        TalentCategoryPointsInfo(1, points),
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
