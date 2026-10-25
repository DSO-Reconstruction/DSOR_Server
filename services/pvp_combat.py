"""Mutually opted-in player-versus-player combat scheduling."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.gameplay.combat_entities import (
    CombatEntity,
    impact_status_effects,
    scaled_skill_damage,
)
from dso_server.runtime.gameplay_scheduler import GameplayDeadlineHandle
from dso_server.runtime.transport import Session
from dso_server.gameplay.pvp_match_state import MatchPhase
from dso_server.gameplay.local_combat_state import (
    damage_type_ids,
    deterministic_combat_roll,
    parse_disabled_actor_features,
)


class PVPCombatService:
    """Validate, schedule, and resolve player-versus-player skill hits."""

    @staticmethod
    def _pvp_round_token(s):
        match = getattr(s, "gameplay_match", None)
        return (id(match), match.round_index) if match is not None else None

    @staticmethod
    def _pvp_target_is_hostile(source: Session, target: Session) -> bool:
        """Return whether two current map players may harm one another."""

        if (
            source is target
            or not source.connected
            or not target.connected
            or not source.gameplay_pvp_enabled
            or not target.gameplay_pvp_enabled
            or source.gameplay_combat_state is None
            or target.gameplay_combat_state is not source.gameplay_combat_state
            or source.gameplay_presence_callback is None
        ):
            return False
        decision = source.gameplay_presence_callback(
            source, ("pvp_hostile", target)
        )
        # Tests and embedders may provide a minimal presence callback that
        # predates the social query. Mutual PvP/map validation above remains
        # authoritative; the real server returns an explicit group decision.
        return True if decision is None else bool(decision)

    @staticmethod
    def _battleground_hostility(
        source: Session, target: Session
    ) -> bool | None:
        """Decide hostility from the arena roster, or ``None`` outside one.

        Inside a battleground the match's team assignment is the only thing
        that separates friend from foe: a 1v1 puts the two players on
        opposing teams whatever their social group says, and a team mode
        keeps team-mates friendly whether or not they are grouped. Social
        groups therefore never grant protection in the arena; two grouped
        friends who queue into a duel must be able to finish it.
        """

        match = getattr(source, "gameplay_match", None)
        if match is None or getattr(target, "gameplay_match", None) is not match:
            return None

        def team_of(session: Session):
            team = getattr(session, "gameplay_match_team", None)
            if team is not None:
                return int(team)
            character = session.gameplay_character
            participant = (
                match.participant(int(character.character_id))
                if character is not None
                else None
            )
            return None if participant is None else int(participant.team)

        source_team = team_of(source)
        target_team = team_of(target)
        if source_team is None or target_team is None:
            return None
        return source_team != target_team

    def _presence_pvp_hostility(
        self, source: Session, target: Session
    ) -> bool:
        """Resolve mutual opt-in, arena teams, and social-group immunity.

        The arena roster wins over the social group: see
        ``_battleground_hostility``. Outside a match, players in one group
        cannot harm each other even with PvP enabled.
        """

        source_character = source.gameplay_character
        target_character = target.gameplay_character
        if (
            source_character is None
            or target_character is None
            or target is source
            or not target.connected
            or not source.gameplay_pvp_enabled
            or not target.gameplay_pvp_enabled
            or target.gameplay_combat_state is not source.gameplay_combat_state
        ):
            return False
        arena_decision = CharacterService._battleground_hostility(
            source, target
        )
        if arena_decision is not None:
            return arena_decision
        source_group = self.social_store.group_for(
            int(source_character.character_id)
        )
        target_group = self.social_store.group_for(
            int(target_character.character_id)
        )
        return not (
            source_group is not None
            and target_group is not None
            and int(source_group.group_id) == int(target_group.group_id)
        )

    @staticmethod
    def _player_disabled_actor_features(s: Session) -> frozenset[str]:
        """Return every ActorFeature currently switched off on this player.

        Called from the head of every MoveCommand, every cast, and once per
        candidate viewer on every 25 Hz monster tick, so on a populated map
        this runs tens of times a second per player. Re-deriving it from raw
        ``StartModifiers`` text each time measured 0.043 ms per feature query
        against a level-100 loadout; the map simulation thread pays that for
        each viewer it evaluates.

        Two layers make it cheap without weakening correctness. The per-effect
        ``ActorFeature`` program is immutable content and is memoized by the
        map (``status_effect_disabled_features``). The per-player union is
        cached against the identity of the live source tuple, which changes
        whenever an effect is added, removed, or expires -- the same event that
        would change the answer.
        """

        match = getattr(s, "gameplay_match", None)
        if match is not None and match.phase != MatchPhase.RUNNING:
            return frozenset({"movement", "skills", "damage"})
        if getattr(s, "gameplay_death_pending", False):
            return frozenset({"movement", "skills", "damage", "targetable"})
        revival_locks = (
            frozenset({"movement", "skills"})
            if time.perf_counter() < getattr(s, "gameplay_revive_movement_until", 0.0)
            else frozenset()
        )
        combat = s.gameplay_combat_state
        if combat is None:
            return revival_locks
        effect_ids = tuple(sorted(
            CharacterService._active_status_effect_ids(s)
            + tuple(
                int(source.effect_id)
                for source
                in CharacterService._active_pvp_victim_modifier_sources(s)
                if hasattr(source, "effect_id")
            )
        ))
        cache_key = (id(combat), effect_ids)
        cached = getattr(s, "gameplay_disabled_feature_cache", None)
        if cached is not None and cached[0] == cache_key:
            return cached[1] | revival_locks
        # Real maps memoize this per effect id. Minimal protocol harnesses
        # supply only ``_status_effect_runtime_program``, so fall back to the
        # same pure parser rather than requiring the full combat object.
        resolve = getattr(combat, "status_effect_disabled_features", None)
        if resolve is None:
            def resolve(effect_id, _combat=combat):
                _interval, _ticks, encoded = (
                    _combat._status_effect_runtime_program(int(effect_id))
                )
                return parse_disabled_actor_features(encoded)
        disabled: set[str] = set()
        for effect_id in effect_ids:
            disabled.update(resolve(effect_id))
        result = frozenset(disabled)
        s.gameplay_disabled_feature_cache = (cache_key, result)
        return result | revival_locks

    @staticmethod
    def _player_victim_feature_locked(
            s: Session, *features: str) -> bool:
        """Evaluate active self-buffs and victim ActorFeature locks."""

        wanted = {str(feature).casefold() for feature in features}
        if not wanted:
            return False
        disabled = CharacterService._player_disabled_actor_features(s)
        return not wanted.isdisjoint(disabled)

    @staticmethod
    def _player_skill_cast_locked(s: Session, skill_index: int) -> bool:
        """Report whether the player's active locks forbid this one cast.

        Stuns and freezes switch off ``RegularSkills``; silence, death and
        scripted sequences switch off ``Skills``. A skill that cleanses the
        groups of every effect behind a ``RegularSkills`` lock is not a
        regular skill to that lock -- that is how Teleport, Dive, Rocket Pack
        and Furious Battle Cry break a stun. ``Skills`` is never bypassed.
        """

        disabled = CharacterService._player_disabled_actor_features(s)
        if "skills" in disabled:
            return True
        if "regularskills" not in disabled:
            return False
        combat = s.gameplay_combat_state
        if getattr(combat, "player_skill_cleanse_groups", None) is None:
            return True
        cleansed = combat.player_skill_cleanse_groups(
            skill_index,
            modifier_sources=(
                CharacterService._active_skill_modifier_sources(s)
            ),
        )
        if not cleansed:
            return True
        # Only debuffs other actors installed are cleansed (see
        # ``_cleanse_player_skill_debuffs``); the player's own sources keep
        # whatever lock they author.
        remaining = CharacterService._active_status_effect_ids(s) + tuple(
            int(source.effect_id)
            for source in CharacterService._active_pvp_victim_modifier_sources(s)
            if hasattr(source, "effect_id")
            and not cleansed & combat._status_groups_for_effect(
                int(source.effect_id)
            )
        )
        return any(
            "regularskills" in combat.status_effect_disabled_features(effect_id)
            for effect_id in remaining
        )

    @staticmethod
    def _cleanse_player_skill_debuffs(
            s: Session, combat, *, skill_index: int,
            modifier_sources) -> None:
        """Remove the debuffs an accepted cast frees its caster from.

        Effects other actors put on this player are held by every viewer's
        victim-effect cache, keyed by that viewer's id for the player: the
        caster's own copy drives the server-side locks and DoT ticks, a
        peer's copy its ADD-versus-replacement bookkeeping. Instance ids are
        per viewer, so each one gets its own no-details removals.
        """

        cleansed = combat.player_skill_cleanse_groups(
            skill_index, modifier_sources=tuple(modifier_sources)
        )
        character = s.gameplay_character
        if not cleansed or character is None:
            return
        remote_actor_id = int(
            s.gameplay_remote_actor_id
            or CharacterService._remote_actor_id(character.character_id)
        )
        viewers = [(LOCAL_PLAYER_ACTOR_ID, s)]
        callback = s.gameplay_presence_callback
        if callback is not None:
            viewers.extend(
                (remote_actor_id, viewer)
                for _actor_id, _position, viewer
                in (callback(s, ("combat_targets",)) or ())
                if viewer is not s and viewer.gameplay_combat_state is combat
            )
        now = time.perf_counter()
        cleansed_own = False
        for actor_id, viewer in viewers:
            removed = []
            with viewer.flow_lock:
                cache = viewer.gameplay_victim_status_effect_instances
                for key, (entry, expires_at) in tuple(cache.items()):
                    if (
                        int(key[0]) == actor_id
                        and float(expires_at) > now
                        and cleansed & combat._status_groups_for_effect(
                            int(entry.effect_id)
                        )
                    ):
                        cache.pop(key)
                        removed.append(entry)
            if not removed:
                continue
            cleansed_own = cleansed_own or viewer is s
            viewer.send_reliable_ordered(
                encode_status_effect_batch(
                    StatusEffect(False, tuple(replace(
                        entry, has_details=False, values=(), field_25=False,
                        field_39=False, field_38=False, field_3c=0xFF,
                        shape_values=(),
                    ) for entry in removed)),
                    actor_id=actor_id,
                ),
                track=True,
                label="player-skill-cleanse",
            )
        if cleansed_own:
            CharacterService._refresh_player_combat_vitals(
                s, inventory_changed=False,
            )
        # The caster's own running effects of those groups end too.
        # EVIDENCE (data): IronDwarfEnd's only effect is
        # StatusEffectGroupCleanse "Transform", the group of the
        # skill_irondwarf_transform its IronDwarf started on the caster.
        # FAILURE: only effects others had put on the player were searched,
        # so casting IronDwarfEnd left the transformation running.
        CharacterService._end_player_runtime_effects(
            s,
            lambda entry: bool(
                cleansed & combat._status_groups_for_effect(int(entry.effect_id))
            ),
            label="player-skill-cleanse-own",
        )

    @staticmethod
    def _player_is_untargetable(s: Session) -> bool:
        """Report whether monsters should stop treating this player as prey.

        Deliberately narrower than "cannot be damaged". Authored defensive
        procs disable only the Damage feature and are meant to nullify incoming
        damage, not to make their owner disappear -- monsters keep chasing and
        swinging, the hits simply do nothing. Only an explicit `Targetable`
        lock, or the movement+skills lock that death, revival and hiding use,
        removes a player from the aggro set.
        """

        if CharacterService._player_victim_feature_locked(s, "targetable"):
            return True
        return CharacterService._player_victim_feature_locked(
            s, "movement"
        ) and CharacterService._player_victim_feature_locked(s, "skills")

    @staticmethod
    def _player_status_immunity_groups(s: Session) -> frozenset[str]:
        """Return authored debuff groups blocked by active player effects."""

        combat = s.gameplay_combat_state
        if combat is None:
            return frozenset()
        result = set()
        sources = CharacterService._active_skill_modifier_sources(s)
        for source in sources:
            if not hasattr(source, "effect_id"):
                continue
            _interval, _ticks, start_modifiers = (
                combat._status_effect_runtime_program(int(source.effect_id))
            )
            for raw in str(start_modifiers).split(";"):
                kind, separator, encoded = raw.partition(":")
                if (
                    separator
                    and kind.strip().casefold()
                    == "statuseffectimmunitygroup"
                ):
                    group = encoded.partition(",")[0].strip().casefold()
                    if group:
                        result.add(group)
        return frozenset(result)

    @staticmethod
    def _player_crowd_control_returns(
            s: Session, combat, definitions, *, pvp=False) -> tuple:
        """Refuse a crowd control inside the immunity its predecessor left.

        CONTRACT: when a victim effect whose StopModifiers grant an immunity
        (debuff_cc_stun / _freeze / _petrify -> debuff_cc_stun_immunity,
        StatusEffectImmunityGroup G_debuff_stun and stun, PvE D:0.5) ends on
        the player, effects of those groups are refused for that long. The
        window is armed when the effect is accepted: it opens at its expiry.
        PvP is excluded (DisableOnRuleSet:PvP).
        EVIDENCE (data): _Template_StatusEffect StopModifiers of the three
        CC effects; SEE: LocalCombatState._arm_monster_status_immunity, the
        same rule on monsters, which players lacked.
        """

        content = getattr(combat, "_status_effect_content", None)
        if pvp or content is None:
            return tuple(definitions)
        now = time.perf_counter()
        windows = {
            group: window
            for group, window in getattr(
                s, "gameplay_player_cc_immunity", {}
            ).items()
            if window[1] > now
        }
        result = []
        for definition in definitions:
            groups, immunity = content(str(definition.resource_id))
            if any(
                windows[group][0] <= now
                for group in groups & windows.keys()
            ):
                continue
            result.append(definition)
            if immunity is None or int(definition.duration_ticks) <= 0:
                continue
            blocked_groups, seconds = immunity
            start = now + int(definition.duration_ticks) / float(
                SERVER_TICKS_PER_SECOND
            )
            for group in blocked_groups:
                previous = windows.get(group)
                if previous is None or previous[0] < start:
                    windows[group] = (start, start + float(seconds))
        s.gameplay_player_cc_immunity = windows
        return tuple(result)

    @staticmethod
    def _filter_player_status_effects(s: Session, definitions, *, pvp=False) -> tuple:
        """Apply socket immunity and authored status-group duration changes."""

        definitions = tuple(
            replace(definition, duration_ticks=definition.pvp_duration_ticks)
            if pvp and getattr(definition, "pvp_duration_ticks", None) is not None
            else definition
            for definition in definitions
        )
        combat = s.gameplay_combat_state
        blocked = CharacterService._player_status_immunity_groups(s)
        evaluation = getattr(
            s, "gameplay_socket_effect_evaluation", None
        )
        has_duration_rules = bool(
            getattr(evaluation, "status_group_durations", ())
        )
        if combat is None:
            return tuple(definitions)
        if not blocked and not has_duration_rules:
            return CharacterService._player_crowd_control_returns(
                s, combat, definitions, pvp=pvp
            )
        result = []
        for definition in definitions:
            groups = combat._status_groups_for_effect(
                int(definition.effect_id)
            )
            if blocked & groups:
                continue
            bonus_seconds = (
                evaluation.status_group_duration_bonus(groups)
                if evaluation is not None else 0.0
            )
            if bonus_seconds != 0.0:
                definition = replace(
                    definition,
                    duration_ticks=max(
                        0,
                        int(definition.duration_ticks)
                        + round(
                            bonus_seconds * SERVER_TICKS_PER_SECOND
                        ),
                    ),
                )
            result.append(definition)
        return CharacterService._player_crowd_control_returns(
            s, combat, result, pvp=pvp
        )

    @staticmethod
    def _active_pvp_victim_modifier_sources(
            target: Session, *, actor_id=LOCAL_PLAYER_ACTOR_ID) -> tuple[object, ...]:
        """Return live debuffs installed on the target player's actor."""

        # Modifier reads also run inside the caller's session lock. As with
        # runtime/item/socket sources, snapshot immutable entries without
        # re-entering that non-reentrant lock. Impact/round cleanup owns cache
        # mutation; expired entries never participate in this snapshot.
        now = time.perf_counter()
        return tuple(
            entry
            for (entry_actor_id, _effect_id), (entry, expires_at)
            in tuple(target.gameplay_victim_status_effect_instances.items())
            if int(entry_actor_id) == int(actor_id) and float(expires_at) > now
        )

    @staticmethod
    def _pvp_damage_type_multiplier(stats, encoded: str) -> float:
        """Blend target mitigation for authored mixed-damage skills."""

        weighted = []
        for raw in str(encoded or "Physical").replace(",", ";").split(";"):
            token, separator, raw_weight = raw.strip().partition(":")
            try:
                weight = float(raw_weight) if separator else 1.0
            except ValueError:
                weight = 1.0
            if weight > 0.0:
                weighted.append((token or "Physical", weight))
        if not weighted:
            weighted = [("Physical", 1.0)]
        total = sum(weight for _name, weight in weighted)
        return sum(
            stats.incoming_damage_multiplier(name) * weight
            for name, weight in weighted
        ) / max(1e-9, total)

    @staticmethod
    def _snapshot_pvp_skill_targets(s: Session, combat, *, skill_index=None, modifier_sources=()):
        """Resolve hostility/session ownership outside the combat lock."""
        if not s.gameplay_pvp_enabled or s.gameplay_presence_callback is None:
            return (), ()
        targets = s.gameplay_presence_callback(s, ("combat_targets",)) or ()
        owners = {int(actor_id): owner for actor_id, _position, owner in targets}
        with combat.lock:
            summons = tuple(getattr(combat, "player_summons", {}).values())
        frame = combat.encounter.coordinate_frame
        targets = (*targets, *tuple(
            (summon.actor_id, frame.world_to_wire(summon.position), owners[summon.owner_actor_id])
            for summon in summons if summon.owner_actor_id in owners
        ))
        geometry = combat._load_player_skill_geometry(skill_index) if skill_index is not None else None
        if geometry is not None:
            geometry = combat._modified_player_skill_geometry(geometry, modifier_sources)
        accepted, entities = [], []
        for actor_id, position, owner in targets:
            if (not CharacterService._pvp_target_is_hostile(s, owner)
                    or owner.gameplay_combat_state is not combat
                    or CharacterService._pvp_actor_damage_locked(owner, actor_id)):
                continue
            actor_id = int(actor_id)
            impact_count = getattr(geometry, "impact_count", 1)
            if geometry is not None:
                victim = CharacterService._active_pvp_victim_modifier_sources(
                    owner, actor_id=actor_id if CharacterService._pvp_actor_is_summon(owner, actor_id)
                    else LOCAL_PLAYER_ACTOR_ID)
                if victim:
                    impact_count = getattr(combat._modified_player_skill_geometry(geometry, victim), "impact_count", 1)
            accepted.append((actor_id, tuple(position), owner))
            entities.append(CombatEntity(actor_id, frame.wire_to_world(position), 0.75,
                                         impact_count=impact_count))
        return tuple(accepted), tuple(entities)

    @staticmethod
    def _prepare_pvp_skill_hits(
            s: Session, combat, skill, *,
            source_position: tuple[float, float, float],
            source_actor_id: int,
            player_damage_range: tuple[float, float],
            player_critical_hit_chance: float = 0.0,
            player_critical_damage_multiplier: float = 2.0,
            attack_speed_multiplier: float,
            modifier_sources: tuple[object, ...],
            explicit_target_actor_ids: tuple[int, ...] = (),
            target_position: tuple[float, float, float] | None = None,
            now: float | None = None, hit_plan=None, target_snapshot=None
    ) -> tuple[GameplayDeadlineHandle, ...]:
        """Queue authoritative per-projectile hits against hostile actors."""

        if (
            not s.gameplay_pvp_enabled
            or s.gameplay_presence_callback is None
        ):
            return ()
        if now is None:
            now = time.perf_counter()
        targets, entities = (
            target_snapshot if target_snapshot is not None else
            CharacterService._snapshot_pvp_skill_targets(
                s, combat, skill_index=skill.skill_index, modifier_sources=modifier_sources)
        )
        if hit_plan is None:
            projectile = isinstance(skill, (BulletSkill, TargetBulletSkill, TargetPointBulletSkill))
            direction = getattr(skill, "direction", (
                -math.sin(float(skill.aim_angle)), 0.0, -math.cos(float(skill.aim_angle)), 0.0))
            with combat.lock:
                hit_plan = combat.plan_entity_skill_hits(
                    skill_index=skill.skill_index, player_position=source_position,
                    projectile=projectile, direction=direction, aim_angle=float(skill.aim_angle),
                    target_position=target_position, explicit_target_actor_ids=explicit_target_actor_ids,
                    modifier_sources=modifier_sources, additional_entities=entities,
                    attack_speed_multiplier=attack_speed_multiplier, now=now,
                    require_explicit_target=isinstance(skill, TargetBulletSkill),
                )
        if hit_plan is None:
            return ()
        geometry = hit_plan.geometry
        target_by_id = {actor_id: (position, owner) for actor_id, position, owner in targets}
        timers = []
        for impact_index, impact in enumerate(hit_plan.impacts):
            actor_id = impact.target.actor_id
            if actor_id not in target_by_id:
                continue
            position, target_session = target_by_id[actor_id]
            delay = impact.delay
            projectile_index = impact.projectile_index
            target_is_summon = CharacterService._pvp_actor_is_summon(target_session, actor_id)
            victim_sources = (
                CharacterService._active_pvp_victim_modifier_sources(
                    target_session, actor_id=actor_id if target_is_summon else LOCAL_PLAYER_ACTOR_ID
                )
            )
            target_geometry = (
                combat._modified_player_skill_geometry(
                    geometry, victim_sources
                )
                if victim_sources
                else geometry
            )
            target_character = target_session.gameplay_character
            if target_is_summon:
                target_stats = CharacterService._pvp_summon_stats(target_session, actor_id)
            else:
                target_stats = calculate_player_combat_stats(
                    combat.encounter.static_database_path,
                    cache_owner=target_session,
                    class_name=CLASS_NAMES.get(
                        int(getattr(target_character, "class_id", 0)), "mage"
                    ),
                    level=max(1, int(getattr(target_character, "level", 1))),
                    inventory=target_session.gameplay_inventory,
                    modifier_sources=CharacterService._active_skill_modifier_sources(
                        target_session
                    ),
                )
            mitigation = CharacterService._pvp_damage_type_multiplier(
                target_stats, target_geometry.damage_type
            )
            roll_bits = (
                int(skill.cast_id) * 1_103_515_245
                + int(source_actor_id) * 12_345
                + actor_id * 97_531
                + projectile_index * 257
            ) & 0xFFFFFFFF
            minimum_damage = min(
                float(player_damage_range[0]),
                float(player_damage_range[1]),
            )
            maximum_damage = max(
                float(player_damage_range[0]),
                float(player_damage_range[1]),
            )
            rolled_damage = minimum_damage + (
                maximum_damage - minimum_damage
            ) * (roll_bits / 0x1_0000_0000)
            critical_roll_bits = (
                roll_bits * 1_664_525
                + 1_013_904_223
                + int(skill.skill_index) * 257
            ) & 0xFFFFFFFF
            critical_hit = (
                float(player_critical_hit_chance) > 0.0
                and critical_roll_bits / 0x1_0000_0000
                < min(1.0, float(player_critical_hit_chance))
            )
            block_roll = deterministic_combat_roll(
                0x50565042,  # "PVPB" domain separator
                int(skill.cast_id),
                int(source_actor_id),
                int(actor_id),
                int(skill.skill_index),
                int(skill.server_tick),
                projectile_index,
            )
            target_block_chance = float(
                getattr(target_stats, "block_chance", 0.0)
            )
            target_block_reduction = float(
                getattr(target_stats, "block_damage_reduction", 0.0)
            )
            blocked = (
                target_block_chance > 0.0
                and block_roll < min(1.0, target_block_chance)
            )
            block_multiplier = (
                1.0
                - max(
                    0.0,
                    min(1.0, target_block_reduction),
                )
                if blocked
                else 1.0
            )
            wearer_low_health = CharacterService._player_is_low_health(s)
            status_group_test = getattr(
                combat, "status_sources_have_group", None
            )
            frost_hit = bool(
                status_group_test(victim_sources, ("frost", "freeze"))
                if status_group_test is not None
                else any(
                    token in str(
                        getattr(source, "resource_id", "")
                    ).casefold()
                    for source in victim_sources
                    for token in ("frost", "freeze", "frozen")
                )
            )
            impact_effects = {
                effect.resource_id: effect
                for effect in impact_status_effects(
                    target_geometry, projectile=isinstance(
                        skill, (BulletSkill, TargetBulletSkill, TargetPointBulletSkill)),
                )
                if combat._effect_applies(
                    effect,
                    cast_id=int(skill.cast_id) + actor_id + projectile_index * 257,
                    killed=False,
                    critical_hit=critical_hit,
                    low_health=wearer_low_health,
                    frost_hit=frost_hit,
                )
            }
            # Only the hit half of the caster column belongs to an impact.
            # ``skillstart`` effects already went out with the cast; sending
            # them again per player hit charged Blood Mage's health cost once
            # for every projectile that landed.
            user_effects = tuple(
                effect
                for effect in getattr(geometry, "user_status_effects", ())
                if normalize_socket_trigger(effect.trigger) in {
                    "", "hit", "hitcritical", "hitlowhealth", "hitfrost",
                }
                and combat._effect_applies(
                    effect,
                    cast_id=int(skill.cast_id) + actor_id + projectile_index * 257,
                    killed=False,
                    critical_hit=critical_hit,
                    low_health=wearer_low_health,
                    frost_hit=frost_hit,
                )
            )
            key = (
                "pvp-hit", int(source_actor_id), int(skill.cast_id), actor_id, projectile_index
            )
            pending = SimpleNamespace(
                round_token=CharacterService._pvp_round_token(s),
                combat=combat,
                target_session=target_session,
                target_is_summon=target_is_summon,
                target_actor_id=actor_id,
                source_actor_id=int(source_actor_id),
                skill_index=int(skill.skill_index),
                cast_id=int(skill.cast_id),
                projectile_index=projectile_index,
                impact_index=impact_index,
                server_tick=(
                    int(skill.server_tick)
                    + round(delay * SERVER_TICKS_PER_SECOND)
                ) & 0xFFFFFFFF,
                position=position,
                damage=scaled_skill_damage(
                    rolled_damage, target_geometry.damage_modifier,
                    mitigation=mitigation, block=block_multiplier,
                    critical=(max(1.0, float(player_critical_damage_multiplier))
                              if critical_hit else 1.0),
                ),
                victim_status_effects=tuple(impact_effects.values()),
                user_status_effects=user_effects,
                frost_hit=frost_hit,
                damage_type=target_geometry.damage_type,
                player_damage_range=tuple(
                    float(value) for value in player_damage_range
                ),
                critical_hit=critical_hit,
                blocked=blocked,
            )
            generation = s.gameplay_chase_generation
            # Target discovery intentionally happens without ``flow_lock``.
            # Protect only the local pending/timer registration and recheck
            # that this map generation is still current.
            with s.flow_lock:
                if (
                    generation != s.gameplay_chase_generation
                    or not s.gameplay_pvp_enabled
                    or s.gameplay_combat_state is not combat
                ):
                    continue
                old = s.gameplay_projectile_timers.pop(key, None)
                s.gameplay_pending_pvp_hits[key] = pending
                timer = s.gameplay_deadline_timer(
                    max(0.0, delay),
                    CharacterService._resolve_pvp_skill_hit,
                    args=(s, generation, key),
                )
                timer.absolute_deadline = now + max(0.0, delay)
                timer.daemon = True
                s.gameplay_projectile_timers[key] = timer
            if old is not None:
                old.cancel()
            timers.append(timer)
        return tuple(timers)

    @staticmethod
    def _pvp_lethal_damage_allowed(source: Session, target: Session) -> bool:
        """Use one death policy for player spells, DoTs, and owned summons."""

        return CharacterService._pvp_match_opponents(source, target) or (
            getattr(target, "gameplay_respawn_allowed", False)
            and getattr(target, "gameplay_match", None) is None
        )

    @staticmethod
    def _pvp_target_world_position(target: Session, combat, fallback, *, actor_id=None):
        """Kill snaps the body to a world position, not Move's relative vector."""

        if actor_id is not None and CharacterService._pvp_actor_is_summon(target, actor_id):
            with combat.lock:
                summon = combat.player_summons.get(int(actor_id))
                if summon is not None:
                    return summon.position
        with target.flow_lock:
            move = target.gameplay_last_move
            position = move.relative_position if move is not None else fallback
        return combat.encounter.coordinate_frame.wire_to_world(position)

    @staticmethod
    def _resolve_pvp_skill_hit(
            s: Session, generation: int, timer_key) -> None:
        with s.flow_lock:
            s.gameplay_projectile_timers.pop(timer_key, None)
            pending = s.gameplay_pending_pvp_hits.pop(timer_key, None)
            if (
                pending is None
                or not s.connected
                or generation != s.gameplay_chase_generation
                or not s.gameplay_pvp_enabled
            ):
                return
        target = pending.target_session
        round_token = CharacterService._pvp_round_token(s)
        if (
            getattr(pending, "round_token", round_token) != round_token
            or not CharacterService._pvp_target_is_hostile(s, target)
            or target.gameplay_combat_state is not pending.combat
            or (getattr(pending, "target_is_summon", False)
                and pending.target_actor_id not in pending.combat.player_summons)
            or CharacterService._pvp_actor_damage_locked(target, pending.target_actor_id)
            or (not getattr(pending, "target_is_summon", False)
                and getattr(target, "gameplay_gm_invulnerable", False))
        ):
            return
        target_is_summon = CharacterService._pvp_actor_is_summon(target, pending.target_actor_id)
        match_opponents = not target_is_summon and CharacterService._pvp_match_opponents(s, target)
        lethal_allowed = target_is_summon or CharacterService._pvp_lethal_damage_allowed(s, target)
        # Roll the caster's set bonuses once, then filter against immunity at
        # impact. Neither an observer's equipment nor immunity at cast time
        # should decide which effects reach the victim.
        set_effects = CharacterService._item_set_procs_for_skill(
            s, pending.skill_index
        ).get("victim", ())
        effects = CharacterService._filter_pvp_actor_effects(
            target, pending.target_actor_id,
            tuple({effect.resource_id: effect for effect in (
                *pending.victim_status_effects, *set_effects,
            )}.values()),
        )
        position = CharacterService._pvp_target_world_position(
            target, pending.combat, pending.position, actor_id=pending.target_actor_id
        )
        if not target_is_summon and int(pending.damage) > 0:
            CharacterService._cancel_pending_logout(target, "attacked in PvP")
        with pending.combat.lock:
            pools = pending.combat.player_summon_health if target_is_summon else pending.combat.player_health
            health = pools.get(pending.target_actor_id)
            if health is None:
                return
            previous, maximum = health
            floor = 0 if lethal_allowed else 1
            current = max(floor, int(previous) - int(pending.damage))
            damage = int(previous) - current
            if previous <= 0 or (damage <= 0 and not pending.blocked and not effects):
                return
            killed = lethal_allowed and current <= 0
            health[0] = current
            if not target_is_summon:
                pending.combat.note_player_attacked(pending.target_actor_id, now=time.perf_counter())
            if target_is_summon and not killed:
                CharacterService._record_pvp_summon_effects(pending.combat, pending.target_actor_id, effects)
            resolution = DamageResolution(
                target_actor_id=pending.target_actor_id,
                attacker_actor_id=pending.source_actor_id,
                current_health=current,
                maximum_health=int(maximum),
                damage=damage,
                killed=killed,
                position=position,
                server_tick=pending.server_tick,
                hit_tags=damage_type_ids(pending.damage_type),
                skill_index=pending.skill_index,
                victim_status_effects=effects,
                source_actor_id=pending.source_actor_id,
                critical_hit=bool(pending.critical_hit),
                blocked=bool(pending.blocked),
            )
        with target.flow_lock:
            if not target_is_summon and target.gameplay_inventory is not None:
                target.gameplay_inventory.health_points = float(current)
        if not target_is_summon and damage > 0:
            CharacterService._cancel_respawn_protection(s)
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(
                s, ("pvp_combat_resolution", resolution)
            )
        if match_opponents and s.gameplay_match_callback is not None:
            s.gameplay_match_callback(s, ("match_damage", damage))
            if killed:
                s.gameplay_match_callback(s, ("match_kill", target))
        socket_seed = (
            int(pending.cast_id) ^ int(pending.target_actor_id) * 257
            ^ int(getattr(pending, "projectile_index", 0)) * 65_537
        )
        CharacterService._dispatch_socket_event(
            s,
            "hit",
            server_tick=int(pending.server_tick),
            event_seed=socket_seed,
        )
        if CharacterService._player_is_low_health(s):
            CharacterService._dispatch_socket_event(
                s,
                "hitlowhealth",
                server_tick=int(pending.server_tick),
                event_seed=socket_seed,
            )
        if bool(pending.critical_hit):
            CharacterService._dispatch_socket_event(
                s,
                "hitcritical",
                server_tick=int(pending.server_tick),
                event_seed=socket_seed,
            )
        if bool(pending.frost_hit):
            CharacterService._dispatch_socket_event(
                s,
                "hitfrost",
                server_tick=int(pending.server_tick),
                event_seed=socket_seed,
            )
        if bool(pending.blocked):
            CharacterService._dispatch_socket_event(
                target,
                "hitblocked",
                server_tick=int(pending.server_tick),
                event_seed=socket_seed,
            )
        if not target_is_summon:
            CharacterService._update_low_health_socket_state(
                target,
                current_health=int(resolution.current_health),
                maximum_health=int(resolution.maximum_health),
                server_tick=int(pending.server_tick),
                event_seed=int(pending.source_actor_id),
            )
        CharacterService._send_player_skill_status_effects(
            s,
            pending.user_status_effects,
            server_tick=int(pending.server_tick),
            label="player-pvp-impact-status-effects",
        )
        if killed and target_is_summon:
            CharacterService._expire_player_summon(
                target, target.gameplay_chase_generation, ("pvp-summon-kill", pending.target_actor_id),
                pending.target_actor_id,
            )
        if not killed:
            CharacterService._schedule_pvp_status_damage(
                s, generation, pending, effects,
            )

    @staticmethod
    def _schedule_pvp_status_damage(
            s: Session, generation: int, pending, definitions) -> None:
        """Start database-authored PvP DoT programs after the impact."""

        for definition in definitions:
            # Bind damage to the victim's installed instance. Cleansing or a
            # replacement by another caster must stop this particular DoT.
            with pending.target_session.flow_lock:
                running = pending.target_session.gameplay_victim_status_effect_instances.get(
                    (pending.target_actor_id if CharacterService._pvp_actor_is_summon(
                        pending.target_session, pending.target_actor_id) else LOCAL_PLAYER_ACTOR_ID,
                     int(definition.effect_id))
                )
            if running is None:
                continue
            entry, expires_at = running
            if int(entry.field_20) != int(pending.source_actor_id):
                continue
            program = pending.combat.monster_status_damage_program(definition)
            if program is None:
                continue
            interval, coefficient, damage_type, relative = program
            state = SimpleNamespace(
                round_token=getattr(pending, "round_token", CharacterService._pvp_round_token(s)),
                target_session=pending.target_session,
                target_is_summon=CharacterService._pvp_actor_is_summon(pending.target_session, pending.target_actor_id),
                target_actor_id=int(pending.target_actor_id),
                source_actor_id=int(pending.source_actor_id),
                skill_index=int(pending.skill_index),
                position=tuple(pending.position),
                player_damage_range=tuple(pending.player_damage_range),
                definition=definition,
                coefficient=float(coefficient),
                damage_type=str(damage_type),
                relative=bool(relative),
                interval=float(interval),
                expires_at=float(expires_at),
                instance_id=int(entry.field_04),
            )
            key = (
                "pvp-status-damage",
                state.source_actor_id,
                state.target_actor_id,
                int(definition.effect_id),
            )
            timer = s.gameplay_deadline_timer(
                state.interval,
                CharacterService._tick_pvp_status_damage,
                args=(s, generation, key, state),
            )
            timer.daemon = True
            with s.flow_lock:
                old = s.gameplay_projectile_timers.pop(key, None)
                s.gameplay_projectile_timers[key] = timer
            if old is not None:
                old.cancel()
            timer.start()

    @staticmethod
    def _tick_pvp_status_damage(
            s: Session, generation: int, timer_key, state) -> None:
        with s.flow_lock:
            timer = s.gameplay_projectile_timers.get(timer_key)
            if timer is not None and timer.args[-1] is not state:
                # An already-running callback must not untrack its replacement.
                return
            s.gameplay_projectile_timers.pop(timer_key, None)
            combat = s.gameplay_combat_state
            valid = (
                s.connected
                and s.gameplay_pvp_enabled
                and generation == s.gameplay_chase_generation
                and combat is not None
            )
        target = state.target_session
        round_token = CharacterService._pvp_round_token(s)
        if (
            not valid
            or getattr(state, "round_token", round_token) != round_token
            or not CharacterService._pvp_target_is_hostile(s, target)
            or target.gameplay_combat_state is not combat
            or time.perf_counter() > state.expires_at + 1e-9
            or (getattr(state, "target_is_summon", False)
                and state.target_actor_id not in combat.player_summons)
            or CharacterService._pvp_actor_damage_locked(target, state.target_actor_id)
            or (not getattr(state, "target_is_summon", False)
                and getattr(target, "gameplay_gm_invulnerable", False))
        ):
            return
        target_is_summon = CharacterService._pvp_actor_is_summon(target, state.target_actor_id)
        victim_sources = CharacterService._active_pvp_victim_modifier_sources(
            target, actor_id=state.target_actor_id if target_is_summon else LOCAL_PLAYER_ACTOR_ID
        )
        if not any(
            int(entry.effect_id) == int(state.definition.effect_id)
            and int(entry.field_04) == int(state.instance_id)
            and int(entry.field_20) == int(state.source_actor_id)
            for entry in victim_sources
        ):
            return
        target_character = target.gameplay_character
        if target_is_summon:
            target_stats = CharacterService._pvp_summon_stats(target, state.target_actor_id)
        else:
            target_stats = calculate_player_combat_stats(
                combat.encounter.static_database_path,
                cache_owner=target,
                class_name=CLASS_NAMES.get(
                    int(getattr(target_character, "class_id", 0)), "mage"
                ),
                level=max(1, int(getattr(target_character, "level", 1))),
                inventory=target.gameplay_inventory,
                modifier_sources=(
                    CharacterService._active_skill_modifier_sources(target)
                ),
            )
        raw_damage = (
            sum(state.player_damage_range) * 0.5 * state.coefficient
            if state.relative
            else state.coefficient
        )
        requested_damage = max(
            1,
            int(math.ceil(
                raw_damage
                * CharacterService._pvp_damage_type_multiplier(
                    target_stats, state.damage_type
                )
            )),
        )
        match_opponents = not target_is_summon and CharacterService._pvp_match_opponents(s, target)
        lethal_allowed = target_is_summon or CharacterService._pvp_lethal_damage_allowed(s, target)
        if not target_is_summon and requested_damage > 0:
            CharacterService._cancel_pending_logout(target, "attacked in PvP")
        with combat.lock:
            pools = combat.player_summon_health if target_is_summon else combat.player_health
            health = pools.get(state.target_actor_id)
            if health is None:
                return
            previous, maximum = health
            current = max(0 if lethal_allowed else 1, int(previous) - requested_damage)
            damage = int(previous) - current
            if damage <= 0:
                return
            health[0] = current
            if not target_is_summon:
                combat.note_player_attacked(state.target_actor_id, now=time.perf_counter())
        with target.flow_lock:
            if not target_is_summon and target.gameplay_inventory is not None:
                target.gameplay_inventory.health_points = float(current)
        if not target_is_summon and damage > 0:
            CharacterService._cancel_respawn_protection(s)
        resolution = DamageResolution(
            target_actor_id=state.target_actor_id,
            attacker_actor_id=state.source_actor_id,
            current_health=current,
            maximum_health=int(maximum),
            damage=damage,
            killed=lethal_allowed and current <= 0,
            position=CharacterService._pvp_target_world_position(
                target, combat, state.position, actor_id=state.target_actor_id
            ),
            server_tick=int(s.gameplay_chase_server_tick) & 0xFFFFFFFF,
            hit_tags=damage_type_ids(state.damage_type),
            skill_index=state.skill_index,
            source_actor_id=state.source_actor_id,
            periodic_damage=True,
        )
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(
                s, ("pvp_combat_resolution", resolution)
            )
        if match_opponents and s.gameplay_match_callback is not None:
            s.gameplay_match_callback(s, ("match_damage", damage))
            if resolution.killed:
                s.gameplay_match_callback(s, ("match_kill", target))
        if resolution.killed:
            if target_is_summon:
                CharacterService._expire_player_summon(
                    target, target.gameplay_chase_generation, ("pvp-summon-kill", state.target_actor_id),
                    state.target_actor_id,
                )
            return
        if time.perf_counter() + state.interval <= state.expires_at + 1e-9:
            timer = s.gameplay_deadline_timer(
                state.interval,
                CharacterService._tick_pvp_status_damage,
                args=(s, generation, timer_key, state),
            )
            timer.daemon = True
            with s.flow_lock:
                if (
                    s.connected
                    and generation == s.gameplay_chase_generation
                    and s.gameplay_combat_state is combat
                ):
                    s.gameplay_projectile_timers[timer_key] = timer
                else:
                    return
            timer.start()
