"""Remote-player appearance, summon, and status synchronization."""

from __future__ import annotations
from dso_server.protocol.item_customization_protocol import item_tint

from dso_server.protocol.gameplay_protocol import (
    StatusEffectCommandBatch,
    encode_status_effect_command_batch,
)

from dso_server.gameplay.combat_entities import projectile_emit_delay, shifted_skill_phases
from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.gameplay.player_summon_lifecycle import player_summon_preparation_batch


class PresenceVisualService:
    """Build and publish remote player, summon, and effect snapshots."""

    @staticmethod
    def _forget_remote_visual_status(
            viewer: Session, source_actor_id: int) -> None:
        """Forget client-local visual status state when its actor is gone."""

        source_actor_id = int(source_actor_id)
        with viewer.flow_lock:
            cache = viewer.gameplay_remote_visual_status_effects
            for key in tuple(cache):
                if int(key[0]) == source_actor_id:
                    cache.pop(key, None)

    @staticmethod
    def _observe_remote_visual_status_removals(
            viewer: Session, source_actor_id: int, values) -> None:
        """Retire visual-cache entries removed by ordinary status relay."""

        source_actor_id = int(source_actor_id)
        removed_ids = {
            int(entry.field_04)
            for value in values
            for entry in value.entries
            if not entry.has_details and not entry.field_25
        }
        if not removed_ids:
            return
        with viewer.flow_lock:
            cache = viewer.gameplay_remote_visual_status_effects
            for instance_id in removed_ids:
                cache.pop((source_actor_id, instance_id), None)

    @staticmethod
    def _remote_visual_status_commands(
            viewer: Session, source_actor_id: int, modifier_sources,
            visual_modifier_effects):
        """Reconcile persistent remote spell-VFX selectors exactly once.

        The client handler treats a detailed entry as construction. Reusing
        that entry's instance id before every cast is therefore a duplicate
        ADD, not a harmless refresh. Keep a bounded viewer-local mirror and
        emit fresh creates plus no-details cleanup only when the caster's
        active modifier generation actually changes.
        """

        source_actor_id = int(source_actor_id)
        remapped = CharacterService._remap_player_status_effect(
            StatusEffect(False, tuple(visual_modifier_effects)),
            source_actor_id,
        ).entries
        active_instance_ids = {
            int(source.field_04)
            for source in (*tuple(modifier_sources), *tuple(remapped))
            if isinstance(source, StatusEffectEntry)
        }
        created = []
        removed = []
        with viewer.flow_lock:
            cache = viewer.gameplay_remote_visual_status_effects
            for key, previous in tuple(cache.items()):
                if (
                    int(key[0]) != source_actor_id
                    or int(key[1]) in active_instance_ids
                ):
                    continue
                cache.pop(key, None)
                removed.append(replace(
                    previous,
                    field_25=False,
                    has_details=False,
                    values=(),
                    field_38=False,
                    field_3c=0xFF,
                    shape_values=(),
                ))
            for entry in remapped:
                key = (source_actor_id, int(entry.field_04))
                if key in cache:
                    continue
                canonical = replace(
                    entry,
                    field_20=source_actor_id,
                    field_25=False,
                    has_details=True,
                )
                cache[key] = canonical
                created.append(canonical)
        commands = [
            StatusEffectServerCommand(
                StatusEffect(False, (entry,)), source_actor_id
            )
            for entry in created
        ]
        if removed:
            commands.append(StatusEffectServerCommand(
                StatusEffect(False, tuple(removed)), source_actor_id
            ))
        return tuple(commands)

    @staticmethod
    def _discard_client_location_effects(session: Session) -> None:
        """Empty the client's effect table before it leaves a map.

        The client keeps a location effect until it is told to discard it,
        map changes included, in a table indexed by the low 16 bits of the
        id (LocationEffectManager::RegisterLocationEffect 0x140a1e694). Each
        map instance numbers its effects from 0x10000 again, so one left
        behind is hit by a later NewLocationEffect, which fails to register
        and aborts the client in ~LocationEffect ("!this->IsValid()").
        """

        with session.flow_lock:
            instance_ids = tuple(sorted(session.gameplay_location_effect_ids))
            session.gameplay_location_effect_ids = set()
        if instance_ids:
            session.send_reliable_ordered(
                encode_discard_location_effect_batch(instance_ids, actor_id=0),
                track=True,
                label="map-exit-location-effect-discard",
            )

    @staticmethod
    def _send_player_location_visuals_to(session: Session, visuals) -> None:
        """Reconcile native world effects with one client's registrations."""

        # Actor-hosted auras already render through StatusEffect. Their
        # internal receiver areas must not create duplicate ground VFX.
        visuals = tuple(v for v in visuals if not getattr(v, "follows_owner", False))
        current_by_id = {int(visual.instance_id): visual for visual in visuals}
        with session.flow_lock:
            # Leaving: its table was emptied, and a new effect would outlive
            # the map it belongs to.
            if session.gameplay_exit_transition_started:
                return
            known = set(session.gameplay_location_effect_ids)
            current = set(current_by_id)
            created_ids = tuple(sorted(current - known))
            discarded_ids = tuple(sorted(known - current))
            session.gameplay_location_effect_ids = current

        if discarded_ids:
            session.send_reliable_ordered(
                encode_discard_location_effect_batch(discarded_ids, actor_id=0),
                track=True,
                label="player-location-effect-discard",
            )

        if not created_ids:
            return

        entries_by_owner = {}
        for instance_id in created_ids:
            visual = current_by_id[instance_id]
            viewer_owner_actor_id = CharacterService._viewer_actor_id(
                session, visual.owner_actor_id
            )
            entry = LocationEffectInfo(
                instance_id=int(visual.instance_id),
                effect_type="SphereEffect",
                instance_guid="\0" * 16,
                enabled=False,
                transform_mode=2,
                position=tuple(float(value) for value in visual.position),
                yaw=float(visual.yaw),
                scale=(
                    max(1.0, float(visual.definition.aura_radius)),
                    max(1.0, float(visual.definition.aura_height)),
                ),
                effect_resource_id=visual.definition.resource_id,
                field_78=True,
                field_79=False,
                expires_tick=int(visual.expires_tick) & 0xFFFFFFFF,
                ticks_per_second=round(SERVER_TICKS_PER_SECOND),
                starts_tick=int(visual.starts_tick) & 0xFFFFFFFF,
                values=tuple(
                    float(value)
                    for value in visual.definition.values
                ),
                strength_percent=100,
                owner_actor_id=viewer_owner_actor_id,
                placement=ActorPlacement(
                    0x00010004,
                    viewer_owner_actor_id,
                    0,
                    False,
                    False,
                ),
                field_b0=0,
            )
            entries_by_owner.setdefault(viewer_owner_actor_id, []).append(entry)

        for owner_actor_id, entries in entries_by_owner.items():
            session.send_reliable_ordered(
                encode_new_location_effect_batch(
                    LocationEffectInfoBatch(
                        tuple(entries),
                        actor_id=owner_actor_id,
                    )
                ),
                track=True,
                label="player-location-effect-new",
            )

    @staticmethod
    def _reconcile_active_monster_union(combat, sessions) -> tuple[int, int]:
        """Simulate exactly the alive monsters visible to at least one client.

        Visibility is per session while ``LocalCombatState`` is shared by the
        map. Previously a partial vicinity cull removed actors only from the
        viewer, so the shared active set grew monotonically as players walked
        through a dense map. Movement, A*, attacks and status ticks then kept
        running for actors no client could see.
        """

        sessions = tuple(
            session
            for session in sessions
            if session.gameplay_combat_state is combat
            and session.connected
            and not session.gameplay_presence_departed
        )
        visible: set[int] = set()
        for session in sessions:
            with session.flow_lock:
                visible.update(session.gameplay_spawned_monster_actor_ids)
        with combat.lock:
            wanted = visible.intersection(combat.alive_actor_ids)
            inactive = set(combat.active_monster_actor_ids) - wanted
            if inactive:
                combat.deactivate_monsters(
                    tuple(sorted(inactive)), reset=False
                )
            if wanted:
                combat.activate_monsters(tuple(sorted(wanted)))
            return len(wanted), len(inactive)

    @staticmethod
    def _send_player_summon_despawn_to(
            session: Session, actor_ids: tuple[int, ...]) -> None:
        with session.flow_lock:
            visible = tuple(
                int(actor_id)
                for actor_id in actor_ids
                if int(actor_id) in session.gameplay_summon_actor_ids
            )
            session.gameplay_summon_actor_ids.difference_update(visible)
            for key in tuple(session.gameplay_victim_status_effect_instances):
                if key[0] in visible:
                    session.gameplay_victim_status_effect_instances.pop(key, None)
        if not visible:
            return
        session.send_reliable_ordered(
            encode_discard_monster_batch(visible),
            track=True,
            label="player-summon-despawn",
        )

    @staticmethod
    def _remote_status_snapshot(
            session: Session, actor_id: int) -> StatusEffect:
        """Build only visual status state meaningful on a remote actor."""

        with session.flow_lock:
            ride = copy.deepcopy(session.gameplay_mount_ride)
            cast = copy.deepcopy(session.gameplay_mount_cast)
            persistent = copy.deepcopy(
                session.gameplay_persistent_status_effects
            )
            visual_instance_ids = (
                session.gameplay_active_costume_effect_instance_id,
                session.gameplay_active_pet_effect_instance_id,
                *(instance for _resource, instance
                  in session.gameplay_active_decorations.values()),
            )
        entries = []
        for state in (ride, cast):
            if state is None:
                continue
            entries.extend(
                mount_status_effect(
                    state,
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                    active=True,
                ).entries
            )
        entries.extend(
            persistent[int(instance_id)]
            for instance_id in visual_instance_ids
            if (
                instance_id is not None
                and int(instance_id) in persistent
            )
        )
        return CharacterService._remap_player_status_effect(
            StatusEffect(False, tuple(entries)), actor_id
        )

    @staticmethod
    def _remote_skill_impact_tick(
            session: Session, skill, geometry, context_position,
            hit_frame: int, timing_scale: float,
            pending) -> tuple[int, int]:
        """Resolve the visual deadline independently from target discovery.

        Retail's execution context describes the visual controller, while a
        pending hit describes authoritative damage.  Coupling the two made a
        missed projectile expire on its cast tick and made an untargeted
        Meteor serialize a zero-length shifted phase (a native client assert).
        """

        cast_tick = int(skill.server_tick) & 0xFFFFFFFF
        if isinstance(skill, ShiftedSkill):
            release, impact = (
                shifted_skill_phases(geometry, timing_scale)
                if geometry is not None else (max(1, int(hit_frame)), 1)
            )
            return ((cast_tick + release + impact) & 0xFFFFFFFF, impact)

        scheduled_ticks = []
        if pending is not None:
            value = getattr(pending, "impact_tick", None)
            if value is None:
                value = getattr(pending, "server_tick", None)
            if value is not None:
                scheduled_ticks.append(int(value) & 0xFFFFFFFF)
        # PvP impacts are registered before the presence callback, but are
        # intentionally stored separately from monster impacts.  Use the same
        # deadline so both viewers see the projectile arrive when damage does.
        with session.flow_lock:
            pvp_hits = tuple(session.gameplay_pending_pvp_hits.values())
        for value in pvp_hits:
            if (
                int(getattr(value, "source_actor_id", -1))
                == int(getattr(session, "gameplay_remote_actor_id", -2))
                and int(getattr(value, "cast_id", -1))
                == int(skill.cast_id)
            ):
                scheduled_ticks.append(
                    int(getattr(value, "server_tick")) & 0xFFFFFFFF
                )
        future = tuple(
            (tick - cast_tick) & 0xFFFFFFFF
            for tick in scheduled_ticks
            if 0 < ((tick - cast_tick) & 0xFFFFFFFF) < 0x80000000
        )
        if future:
            return ((cast_tick + min(future)) & 0xFFFFFFFF, 0)

        if geometry is not None and isinstance(
            skill, (BulletSkill, TargetBulletSkill, TargetPointBulletSkill)
        ):
            emit_seconds = projectile_emit_delay(geometry, timing_scale)
            travel_seconds = max(0.0, float(geometry.projectile_lifetime)) * max(
                0.01, float(geometry.skill_timing_multiplier)
            )
            target_position = None
            if isinstance(skill, TargetPointBulletSkill):
                target_position = skill.target_position
            elif isinstance(skill, TargetBulletSkill) and skill.target_positions:
                target_position = skill.target_positions[0]
            velocity = float(geometry.projectile_velocity) / max(
                0.01, float(geometry.skill_timing_multiplier)
            )
            if target_position is not None and velocity > 1e-6:
                travel_seconds = math.dist(
                    tuple(float(value) for value in context_position[:3]),
                    tuple(float(value) for value in target_position[:3]),
                ) / velocity
            elif travel_seconds <= 0.0 and velocity > 1e-6:
                travel_seconds = float(geometry.projectile_range) / velocity
            lifetime_ticks = max(
                int(hit_frame) + 1,
                int(round(
                    (emit_seconds + travel_seconds)
                    * SERVER_TICKS_PER_SECOND
                )),
            )
            return ((cast_tick + lifetime_ticks) & 0xFFFFFFFF, 0)

        return (
            (cast_tick + max(1, int(hit_frame))) & 0xFFFFFFFF,
            0,
        )

    @staticmethod
    def _send_remote_player_skill(
            session: Session, event, viewers, source_move) -> None:
        """Publish one caster-framed, talent-aware spell to map peers."""

        if len(event) >= 7:
            (
                _kind,
                skill,
                pending,
                position,
                source_actor_id,
                modifier_sources,
                skill_speed_multiplier,
            ) = event[:7]
        else:
            _kind, skill, pending, position, source_actor_id = event
            modifier_sources = ()
            skill_speed_multiplier = 1.0
        combat = session.gameplay_combat_state
        geometry = (
            combat._load_player_skill_geometry(skill.skill_index)
            if combat is not None
            else None
        )
        if geometry is not None and modifier_sources:
            geometry = combat._modified_player_skill_geometry(
                geometry, tuple(modifier_sources)
            )
        timing_scale = (
            max(0.1, min(10.0, float(skill_speed_multiplier)))
            if geometry is not None and geometry.scale_skill_timing
            else 1.0
        )
        animation_frame = (
            geometry.loop_start_frame
            if geometry is not None and isinstance(
                skill,
                (
                    ShiftedSkill,
                    BulletSkill,
                    TargetBulletSkill,
                    TargetPointBulletSkill,
                ),
            )
            else (geometry.hit_frame if geometry is not None else 0)
        )
        hit_frame = (
            max(
                0,
                int(round(
                    float(animation_frame)
                    * float(geometry.skill_timing_multiplier)
                    / timing_scale
                )),
            )
            if geometry is not None
            else 0
        )
        if isinstance(skill, ShiftedSkill):
            # ShiftedSkillVisualizer feeds this value directly to
            # CalculateSequenceFPS.  At high attack speed, Python's ties-to-
            # even round(5 / 10) produced zero for Meteor's LoopStartFrame
            # and tripped ``0.0f < phaseLength`` on remote clients.  Retail's
            # high-speed Meteor packet carries one frame here.
            hit_frame = max(1, hit_frame)
        motion_unblock_frame = (
            max(
                0,
                int(round(
                    float(geometry.motion_unblock_frame)
                    * float(geometry.skill_timing_multiplier)
                    / timing_scale
                )),
            )
            if geometry is not None
            else 0
        )
        skill_unblock_frame = (
            max(
                0,
                int(round(
                    float(geometry.skill_unblock_frame)
                    * float(geometry.skill_timing_multiplier)
                    / timing_scale
                )),
            )
            if geometry is not None
            else 0
        )
        context_position = tuple(float(value) for value in position)
        if combat is not None:
            # Skill contexts use absolute database/world coordinates. Move is
            # the exception and remains in the map-relative wire frame.
            context_position = (
                combat.encounter.coordinate_frame.wire_to_world(
                    context_position
                )
            )
        impact_tick, shifted_pre_impact_delay = (
            PresenceVisualService._remote_skill_impact_tick(
                session,
                skill,
                geometry,
                context_position,
                hit_frame,
                timing_scale,
                pending,
            )
        )
        context = SkillExecutionContext(
            impact_tick=impact_tick,
            hit_frame=hit_frame,
            motion_unblock_frame=motion_unblock_frame,
            skill_unblock_frame=skill_unblock_frame,
            reserved=0,
            position=(*context_position, 1.0),
            predicted=False,
        )
        visual_modifier_effects = (
            combat.player_skill_visual_modifier_effects(
                skill.skill_index, tuple(modifier_sources)
            )
            if combat is not None
            else ()
        )
        for viewer in viewers:
            visual_commands = (
                CharacterService._remote_visual_status_commands(
                    viewer,
                    int(source_actor_id),
                    modifier_sources,
                    visual_modifier_effects,
                )
            )
            if visual_commands:
                viewer.send_reliable_ordered(
                    encode_status_effect_command_batch(
                        StatusEffectCommandBatch(visual_commands)
                    ),
                    track=True,
                    label=(
                        f"remote-player-skill-visual-modifiers-"
                        f"{int(source_actor_id)}"
                    ),
                )
            if isinstance(skill, SkillCast):
                playback = SkillServerCommand(
                    skill, int(source_actor_id), context
                )
            elif isinstance(skill, TargetSkillCast):
                playback = TargetSkillServerCommand(
                    TargetSkill(
                        skill.skill_level,
                        skill.skill_index,
                        skill.aim_angle,
                        skill.server_tick,
                        skill.cast_id,
                        CharacterService._viewer_actor_id(
                            viewer, skill.target_actor_id
                        ),
                        context,
                        skill.derived_flag,
                    ),
                    int(source_actor_id),
                )
            elif isinstance(skill, ShiftedSkill):
                playback = ShiftedSkillServerCommand(
                    skill,
                    int(source_actor_id),
                    context,
                    shifted_pre_impact_delay,
                )
            elif isinstance(skill, TargetPointBulletSkill):
                playback = TargetPointBulletSkillServerCommand(
                    skill,
                    int(source_actor_id),
                    context,
                    max(0, int(round(geometry.attack_range)))
                    if geometry is not None
                    else 0,
                )
            elif isinstance(skill, TargetBulletSkill):
                playback = TargetBulletSkillServerCommand(
                    replace(
                        skill,
                        target_actor_id=CharacterService._viewer_actor_id(
                            viewer, skill.target_actor_id
                        ),
                        additional_target_actor_ids=tuple(
                            CharacterService._viewer_actor_id(
                                viewer, target_actor_id
                            )
                            for target_actor_id in (
                                skill.additional_target_actor_ids
                            )
                        ),
                    ),
                    int(source_actor_id),
                    context,
                )
            else:
                playback = BulletSkillServerCommand(
                    BulletSkillServer(
                        skill.skill_level,
                        skill.skill_index,
                        skill.aim_angle,
                        skill.server_tick,
                        skill.cast_id,
                        skill.direction,
                        skill.variant,
                        skill.derived_flag,
                    ),
                    int(source_actor_id),
                    context,
                )
            movement = ()
            if (
                source_move is not None
                and not (
                    geometry is not None
                    and geometry.skill_type.casefold() == "charge"
                    and geometry.resource_id.casefold() != "whirlwind"
                )
            ):
                # Retail co-frames the caster's latest movement snapshot.  It
                # does not blanket-retime it: the supplied PvP Meteor has Move
                # and ShiftedSkill on the same tick with the original duration.
                movement = (
                    MoveServerCommand(
                        source_move,
                        int(source_actor_id),
                    ),
                )
            packet = encode_monster_attack_batch(
                MonsterAttackBatch(movement, (playback,))
            )
            viewer.send_reliable_ordered(
                packet,
                track=True,
                label=(
                    f"remote-player-skill-" f"{int(source_actor_id)}-{skill.cast_id}"
                ),
            )

    @staticmethod
    def _remote_actor_id(character_id: int) -> int:
        # Actor ids are generational: the client indexes ActorManager's fixed
        # array with the low 16 bits, not with the whole integer.  0x18001
        # therefore selected index 0x8001 and was rejected.  Retail remote
        # players in the captured Kingshill session use ids around 0x103c0,
        # whose indices are the valid, compact 0x03c0 range.
        ordinal = max(1, int(character_id) - LOCAL_CHARACTER_ID_BASE + 1)
        if ordinal > 0xFF:
            raise ValueError("character id exceeds local remote-actor capacity")
        # Keep the low indices close to known-good retail values while
        # remaining clear of player=1 and the map's low-index NPC actors.
        return 0x000103C0 + ordinal

    @staticmethod
    def _session_remote_actor_id(server, session: Session) -> int:
        """Allocate one generational actor id for a character map session."""

        with session.flow_lock:
            if session.gameplay_remote_actor_id is not None:
                return int(session.gameplay_remote_actor_id)
            character = session.gameplay_character
        if character is None:
            raise ValueError("remote-player session has no character")
        character_id = int(character.character_id)
        low_index = (
            CharacterService._remote_actor_id(character_id) & 0xFFFF
        )
        with server.gameplay_world_lock:
            generation = (
                int(server.gameplay_remote_actor_generations.get(
                    character_id, 0
                ))
                + 1
            )
            if generation > 0x7FFF:
                generation = 1
            server.gameplay_remote_actor_generations[character_id] = generation
        actor_id = (generation << 16) | low_index
        with session.flow_lock:
            if session.gameplay_remote_actor_id is None:
                session.gameplay_remote_actor_id = actor_id
            return int(session.gameplay_remote_actor_id)

    @staticmethod
    def _remote_animation_for(character, inventory, fallback: str) -> str:
        """Select the equipped armament's client animation set."""

        allocations = (
            tuple(inventory.equipment_slots.values())
            if inventory is not None
            else ()
        )
        has_right_hand = any(5 in slots for slots in allocations)
        has_left_hand = any(6 in slots for slots in allocations)
        has_two_hand = any(
            5 in slots and 6 in slots for slots in allocations
        )
        if not has_right_hand:
            return fallback

        class_id = int(character.class_id)
        female = int(character.gender) == 1
        if class_id in (0, 1):
            class_name = "warrior" if class_id == 0 else "mage"
            gender_part = "_female" if female else ""
            if has_two_hand:
                return f"{class_name}{gender_part}_2h_weapon"
            if has_left_hand:
                return f"{class_name}{gender_part}_1h_weapon_shield"
            return f"{class_name}{gender_part}_1h_weapon"
        if class_id == 2:
            gender_part = "_female" if female else ""
            size = "large" if has_two_hand else "small"
            return f"ranger{gender_part}_{size}_weapon"
        if class_id == 3:
            gender_part = "_female" if female else ""
            if has_two_hand:
                return f"dwarf{gender_part}_2h_weapon"
            if has_left_hand:
                return f"dwarf{gender_part}_1h_weapon_shield"
            return f"dwarf{gender_part}_1h_weapon"
        if class_id == 4:
            return "niwalk_female_1h" if female else "niwalk_1h"
        return fallback

    def _remote_equipment_visuals(
            self, inventory, hidden_skins=None) -> tuple[RemoteEquipmentVisual, ...]:
        if inventory is None or self.static_database is None:
            return ()
        items_by_id = {
            int(item.item_id): item for item in inventory.items
        }
        visuals: dict[int, RemoteEquipmentVisual] = {}
        hidden_skins = hidden_skins or {}
        for item_id, slots in sorted(inventory.equipment_slots.items()):
            visible_slots = tuple(int(slot) for slot in slots if 0 <= int(slot) <= 7)
            if not visible_slots:
                continue
            item = items_by_id.get(int(item_id))
            if item is None:
                continue
            item_skin = str(item.field_f8 or item.field_80 or "")
            visible_slots = tuple(
                slot for slot in visible_slots
                if not (
                    (hidden_skin := str(hidden_skins.get(12 if slot == 7 else slot, "")))
                    and (not item_skin or hidden_skin == item_skin)
                )
            )
            if not visible_slots:
                continue
            try:
                skin = self.static_database.remote_equipment_skin(
                    item.template_id,
                    item.field_f8 or item.field_80,
                )
            except (OSError, ValueError, CharacterSelectionError):
                continue
            color = item_tint(item)
            for slot in visible_slots:
                # RemotePlayerInfo's additional-skin slots 0..7 are the
                # visible helmet, shoulders, torso, gloves, boots, both hands
                # and cloak. Accessories do not create actor geometry.
                visuals[slot] = RemoteEquipmentVisual(slot, (skin,), color)
        return tuple(visuals[slot] for slot in sorted(visuals))

    @staticmethod
    def _remote_emblem_id(inventory) -> str:
        """Return the template id used by the client's remote emblem icon."""

        if inventory is None:
            return ""
        items_by_id = {
            int(item.item_id): item for item in inventory.items
        }
        for item_id, slots in sorted(inventory.equipment_slots.items()):
            if 23 not in tuple(int(slot) for slot in slots):
                continue
            item = items_by_id.get(int(item_id))
            if item is not None:
                return str(item.template_id or "")
        return ""

    @staticmethod
    def _remap_player_status_effect(
            value: StatusEffect, actor_id: int) -> StatusEffect:
        return StatusEffect(
            value.clear_existing,
            tuple(
                replace(entry, field_20=int(actor_id))
                if int(entry.field_20) == LOCAL_PLAYER_ACTOR_ID
                else entry
                for entry in value.entries
            ),
        )

    @staticmethod
    def _send_player_summon_spawn_to(
            session: Session, spawn) -> None:
        """Send the retail lifecycle appropriate for each friendly summon.

        Dwarf turrets are stationary constructions: retail prepares their
        health/faction/aura actor and publishes a fixed transform before the
        ``NewMonster`` command.  Mobile summons such as the Mage Guardians are
        ordinary friendly monsters.  Sending the turret-only pre-registration
        commands for those actors makes the client consume movement/state for
        an actor that does not exist yet and stops the ordered stream.
        """

        if spawn.discarded_actor_ids:
            CharacterService._send_player_summon_despawn_to(
                session, spawn.discarded_actor_ids
            )
        for summon in spawn.actors:
            for group in summon.spawn_groups:
                viewer_owner_actor_id = CharacterService._viewer_actor_id(
                    session, summon.owner_actor_id
                )
                group = CharacterService._summon_group_for_session(session, summon, group)
                actor_id = int(group.actor_ids[0])
                stationary_construction = bool(
                    summon.definition.stationary
                )
                if stationary_construction:
                    session.send_reliable_ordered(
                        encode_monster_update_batch(
                            player_summon_preparation_batch(
                                group,
                                summon.definition.aura,
                                owner_actor_id=viewer_owner_actor_id,
                                instance_id=(summon.initial_aura_instance_id),
                            )
                        ),
                        track=True,
                        label=f"player-summon-prepare-{actor_id}",
                    )
                vicinity_packet = encode_actors_enter_vicinity_batch(
                    group.vicinity
                )
                session.send_reliable_ordered(
                    vicinity_packet,
                    track=True,
                    label=f"player-summon-vicinity-{actor_id}",
                )
                if stationary_construction:
                    # Retail publishes this stationary transform between
                    # vicinity registration and NewMonster; its second
                    # orientation value fixes a turret's sustained-fire
                    # direction. Mobile summons receive their first transform
                    # only after NewMonster has registered the actor.
                    combat = session.gameplay_combat_state
                    spawn_command = group.spawn_batch.commands[0]
                    spawn_tick = int(spawn_command.value.field_c4)
                    wire_position = (
                        combat.encounter.coordinate_frame.world_to_wire(
                            summon.position
                        )
                        if combat is not None
                        else summon.position
                    )
                    session.send_reliable_ordered(
                        encode_move_batch(
                            MoveBatch(
                                (
                                    MoveServerCommand(
                                        Move.from_values(
                                            wire_position,
                                            0.0,
                                            0.0,
                                            float(summon.yaw),
                                            spawn_tick,
                                            0,
                                        ),
                                        actor_id,
                                    ),
                                )
                            )
                        ),
                        track=True,
                        label=f"player-summon-transform-{actor_id}",
                    )
                packet, packet_bits = encode_monster_spawn_batch(
                    group.spawn_batch
                )
                if len(packet) > TAIL_FRAG_SIZE:
                    session.send_reliable_ordered_split(
                        packet,
                        total_bit_length=packet_bits,
                        track=True,
                        label_prefix=(
                            f'player-summon-{actor_id}-'
                            f'player-summon-{actor_id}'
                        ),
                    )
                else:
                    session.send_reliable_ordered(
                        (packet, packet_bits),
                        track=True,
                        label=f"player-summon-spawn-{actor_id}",
                    )
                with session.flow_lock:
                    session.gameplay_summon_actor_ids.add(actor_id)

    @staticmethod
    def _summon_group_for_session(session, summon, group):
        owner = session if session.gameplay_remote_actor_id == summon.owner_actor_id else None
        if owner is None and session.gameplay_presence_callback is not None:
            targets = session.gameplay_presence_callback(session, ("combat_targets",)) or ()
            owner = next((s for actor_id, _position, s in targets
                          if actor_id == summon.owner_actor_id), None)
        hostile = owner is not None and CharacterService._pvp_target_is_hostile(session, owner)
        team = getattr(owner, "gameplay_match_team", None) if owner is not None else None
        health = None
        combat = session.gameplay_combat_state
        if combat is not None:
            with combat.lock:
                pool = getattr(combat, "player_summon_health", {}).get(summon.actor_id)
                health = tuple(pool) if pool is not None else None
        return CharacterService._player_summon_group_for_viewer(
            group, source_owner_actor_id=summon.owner_actor_id,
            viewer_owner_actor_id=CharacterService._viewer_actor_id(session, summon.owner_actor_id),
            hostile=hostile, team_id=team, health=health,
        )

    @staticmethod
    def _player_summon_group_for_viewer(
            group, *, source_owner_actor_id: int,
            viewer_owner_actor_id: int, hostile=False, team_id=None, health=None):
        """Remap a construction's owner identity for one receiving client."""

        source_owner_actor_id = int(source_owner_actor_id)
        viewer_owner_actor_id = int(viewer_owner_actor_id)
        if (source_owner_actor_id == viewer_owner_actor_id and not hostile
                and team_id is None and health is None):
            return group
        commands = []
        for command in group.spawn_batch.commands:
            if isinstance(command, NewMonsterServerCommand):
                commands.append(
                    NewMonsterServerCommand(
                        replace(
                            command.value,
                            field_d8=viewer_owner_actor_id,
                            field_e0=int(team_id) if team_id is not None else 0xFFFFFFFF,
                            field_88=int(health[0]) if health is not None else command.value.field_88,
                            field_90=int(health[1]) if health is not None else command.value.field_90,
                            placement=replace(command.value.placement,
                                              field_00=(0x00010001 if hostile or command.value.category == 2
                                                        else 0x00010004),
                                              field_08=2 if hostile else 0),
                        )
                    )
                )
            elif isinstance(command, StatusEffectServerCommand):
                commands.append(
                    StatusEffectServerCommand(
                        StatusEffect(
                            command.value.clear_existing,
                            tuple(
                                replace(
                                    entry,
                                    field_20=viewer_owner_actor_id,
                                )
                                if int(entry.field_20)
                                == source_owner_actor_id
                                else entry
                                for entry in command.value.entries
                            ),
                        ),
                        command.actor_id,
                    )
                )
            else:
                commands.append(command)
        return replace(
            group,
            spawn_batch=MonsterSpawnBatch(tuple(commands)),
        )

    @staticmethod
    def _send_player_summon_aura_refreshes_to(
            session: Session, commands) -> None:
        """Send rolling aura leases through native StatusEffectCommand."""

        commands = tuple(
            StatusEffectServerCommand(
                StatusEffect(
                    command.value.clear_existing,
                    tuple(
                        replace(
                            entry,
                            field_20=CharacterService._viewer_actor_id(
                                session, entry.field_20
                            ),
                        )
                        for entry in command.value.entries
                    ),
                ),
                command.actor_id,
            )
            for command in commands
        )
        if not commands:
            return
        for command in commands:
            # Retail uses standalone command 0x004f after the actor exists.
            # A 0x002a compound batch is reserved for NewMonster state.
            session.send_reliable_ordered(
                encode_status_effect_batch(
                    command.value,
                    actor_id=int(command.actor_id),
                ),
                track=True,
                label="player-summon-aura-refresh",
            )
