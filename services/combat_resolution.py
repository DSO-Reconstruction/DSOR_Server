"""Combat result serialization and status-effect delivery."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import sqlite3
import math

from dso_server.protocol.gameplay_protocol import (
    StatusEffectCommandBatch,
    encode_status_effect_command_batch,
)

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.gameplay.player_skill_timing import apply_skill_cooldown_effect_locked
from dso_server.gameplay.status_modifier_runtime import (
    load_status_modifier_catalog, parse_modifier_program, resolve_modifier_value,
)


COMBAT_BATCH_COMMAND_BUDGET = 16
WIRE_UINT32_MAX = 0xFFFFFFFF
ATTACK_SPEED_BONUS_STACK = "AttacksPerSecondBonus_stack"


@lru_cache(maxsize=4)
def _load_attack_speed_bonus_effects(database_path: str):
    path = Path(database_path).resolve()
    with sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True
    ) as database:
        stack = LocalCombatState._load_victim_status_effects(
            database,
            f"{ATTACK_SPEED_BONUS_STACK},C:1.0,D:5.0,DP:5.0,ON:Hit",
        )[0]
        buff = LocalCombatState._load_victim_status_effects(
            database,
            "AttacksPerSecondBonus_buff,C:1.0,D:30.0,DP:30.0,ON:Hit",
        )[0]
    # Its authored MaxStackSize child stops the stack at 100. The server
    # consumes that edge instance in the same transaction that applies the
    # linked damage buff.
    return replace(stack, consume_on_max_stack=True), buff


@lru_cache(maxsize=256)
def _keeps_on_death(database_path, resource_id: str) -> bool:
    path = Path(database_path).resolve()
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as database:
        row = database.execute(
            "SELECT COALESCE(KeepOnDeath, 0) FROM _Template_StatusEffect WHERE Id=?",
            (str(resource_id),),
        ).fetchone()
    return bool(row and row[0])


@lru_cache(maxsize=4)
def _kill_heal_programs(database_path: str):
    """Hosted effects whose ``On:Kill`` child restores absolute health.

    Hangman's Pride (``trait_wisdom_all_hpPerKill_trigger``) authors
    ``ActorStatusEffect:addAE,trait_wisdom_all_hpPerKill_effect,C:1.0,
    $0:trait_wisdom_all_hpPerKill,On:Kill`` and the child restores
    ``CurrHealthPoints:$0,absolute``. Item sets and sockets have their own
    event dispatchers; nothing fired these talent rules, so the talent never
    healed. Maps the host's wire id to ``(chance, amount operands,
    parameter bindings)``.
    """

    path = Path(database_path)
    with sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True
    ) as database:
        rows = database.execute(
            "SELECT rowid - 1, Id, COALESCE(StartModifiers, '') "
            "FROM _Template_StatusEffect"
        ).fetchall()
    programs = {str(resource).casefold(): program for _wire, resource, program in rows}
    result = {}
    for wire_id, _resource, program in rows:
        if "on:kill" not in program.casefold():
            continue
        for instruction in parse_modifier_program(program):
            arguments = instruction.arguments
            if (
                instruction.kind != "actorstatuseffect"
                or len(arguments) < 2
                or not arguments[0].casefold().startswith("add")
            ):
                continue
            options = {
                key.strip().casefold(): value.strip()
                for key, separator, value in (
                    argument.partition(":") for argument in arguments[2:]
                )
                if separator
            }
            if options.get("on", "").casefold() != "kill":
                continue
            amounts = tuple(
                child.arguments[0]
                for child in parse_modifier_program(
                    programs.get(arguments[1].strip().casefold(), "")
                )
                if child.kind == "currhealthpoints"
                and len(child.arguments) >= 2
                and child.arguments[1].strip().casefold() == "absolute"
            )
            if not amounts:
                continue
            try:
                chance = max(0.0, min(1.0, float(options.get("c", "1"))))
            except ValueError:
                chance = 0.0
            bindings = tuple(
                (int(key[1:]), value)
                for key, value in options.items()
                if key[:1] == "$" and key[1:].isdigit()
            )
            result.setdefault(int(wire_id), []).append(
                (chance, amounts, bindings)
            )
    return {wire_id: tuple(rules) for wire_id, rules in result.items()}


def _wire_uint32(value: int) -> int:
    """Clamp an authoritative value only at the legacy wire boundary."""

    return min(WIRE_UINT32_MAX, max(0, int(value)))

COMBAT_TEXT_BY_DAMAGE_TYPE = {
    1: "DamageDoneFire",
    2: "DamageDoneIce",
    3: "DamageDoneLightning",
    4: "DamageDoneAndermagic",
    5: "DamageDonePoison",
}


def _load_caster_status_effects(database, value):
    """Parse an item-set ``addUE``/``modUE`` payload as a caster effect.

    A skill's own ``UserStatusEffects`` column defaults to ``skillstart``; a
    set rule that names the caster is the same kind of column and must default
    the same way, or the buff waits for a damage resolution that a pure
    self-buff trigger skill never produces.
    """

    return LocalCombatState._load_victim_status_effects(
        database, value, default_trigger="skillstart"
    )


def _load_caster_aura_owner_effects(database, value):
    """Creator/causer payloads of a caster-targeted item-set aura shell."""

    return LocalCombatState._load_aura_owner_effects(
        database, value, default_trigger="skillstart"
    )


def _combat_text_type(hit_tags, *, periodic_damage: bool) -> str:
    """Select elemental floating text only for periodic effect damage."""

    if not periodic_damage:
        return ""

    for damage_type in hit_tags:
        resource_id = COMBAT_TEXT_BY_DAMAGE_TYPE.get(int(damage_type))
        if resource_id:
            return resource_id
    return ""


class CombatResolutionService:
    """Combat result serialization and status-effect delivery."""

    @staticmethod
    def _cancel_respawn_protection(s: Session) -> None:
        """Remove the temporary revive protection after this player hits."""

        timers = []
        with s.flow_lock:
            instance_id = getattr(
                s, "gameplay_respawn_protection_instance_id", None
            )
            s.gameplay_respawn_protection_instance_id = None
            if instance_id is None:
                return
            runtime = s.gameplay_runtime_status_effects.pop(
                int(instance_id), None
            )
            for prefix in ("player-status-tick", "runtime-effect-expire"):
                timer = s.gameplay_projectile_timers.pop(
                    (prefix, int(instance_id)), None
                )
                if timer is not None:
                    timers.append(timer)
            server_tick = int(s.gameplay_chase_server_tick) & 0xFFFFFFFF
            s.gameplay_disabled_feature_cache = None
        for timer in timers:
            timer.cancel()
        if runtime is None:
            return
        entry = runtime[0]
        removal = CharacterService._consumable_cleanup_entry(
            entry, server_tick=server_tick
        )
        value = StatusEffect(False, (removal,))
        s.send_reliable_ordered(
            encode_status_effect_batch(
                value, actor_id=LOCAL_PLAYER_ACTOR_ID
            ),
            track=True,
            label="respawn-protection-cancelled-on-hit",
        )
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, ("status_effects", (value,)))
        CharacterService._refresh_player_combat_vitals(
            s, inventory_changed=False
        )

    @staticmethod
    def _send_player_heal_transition(
            s: Session, *, current_health: int, maximum_health: int,
            recovered: int, server_tick: int, label: str,
            show_combat_text: bool = True) -> None:
        """Publish a health increase, optionally with healing combat text."""

        if int(recovered) <= 0:
            return
        hit = Hit(
            server_tick=int(server_tick) & 0xFFFFFFFF,
            hit_tags=(),
            field_48=False,
            field_49=False,
            current_health=max(1, int(current_health)),
            maximum_health=max(1, int(maximum_health)),
            absorbed_damage=0,
            shield_damage=0,
            damage=max(1, _wire_uint32(recovered)),
            damage_type=3,
            attacker_actor_id=LOCAL_PLAYER_ACTOR_ID,
            source_actor_id=LOCAL_PLAYER_ACTOR_ID,
            skill_id=0,
            impact_strength=0.0,
            field_80=False,
            field_81=False,
            combat_text_type=("HealingSelf" if show_combat_text else ""),
        )
        packet = encode_combat_batch(CombatBatch((
            HitServerCommand(hit, LOCAL_PLAYER_ACTOR_ID),
        )))
        s.send_reliable_ordered(
            packet,
            track=True,
            label=label,
        )
        CharacterService._update_low_health_socket_state(
            s,
            current_health=int(current_health),
            maximum_health=int(maximum_health),
            server_tick=int(server_tick),
            event_seed=int(recovered),
        )

    @staticmethod
    def _apply_player_damage_healing(
            s: Session, resolutions, *, label: str) -> int:
        """Apply database-authored LifeLeech for authoritative damage."""

        resolutions = tuple(resolutions)
        combat = s.gameplay_combat_state
        character = s.gameplay_character
        inventory = s.gameplay_inventory
        if combat is None or character is None or inventory is None:
            return 0
        if any(
            int(getattr(resolution, "damage", 0)) > 0
            for resolution in resolutions
        ):
            CharacterService._cancel_respawn_protection(s)
        stats = calculate_player_combat_stats(
            combat.encounter.static_database_path,
            cache_owner=s,
            class_name=CLASS_NAMES.get(int(character.class_id), "mage"),
            level=max(1, int(character.level)),
            inventory=inventory,
            modifier_sources=(
                CharacterService._active_skill_modifier_sources(s)
            ),
        )
        recovered_request = 0
        for resolution in resolutions:
            if int(getattr(resolution, "damage", 0)) <= 0:
                continue
            geometry = combat._load_player_skill_geometry(
                int(getattr(resolution, "skill_index", -1))
            )
            if geometry is None:
                continue
            fraction = stats.life_leech_fraction(geometry.resource_id)
            if fraction <= 0.0:
                continue
            recovered_request += max(
                0, int(round(int(resolution.damage) * fraction))
            )
        if recovered_request <= 0:
            return 0
        actor_id = (
            (
                s.gameplay_remote_actor_id
                or CharacterService._remote_actor_id(character.character_id)
            )
            if s.gameplay_presence_callback is not None
            else combat.player_actor_id
        )
        with combat.lock:
            current, maximum, recovered = combat.heal_player_amount(
                actor_id, recovered_request
            )
        if recovered <= 0:
            return 0
        with s.flow_lock:
            inventory.health_points = float(current)
            server_tick = max(
                int(s.gameplay_chase_server_tick),
                int(s.gameplay_last_heal_server_tick) + 1,
            ) & 0xFFFFFFFF
            s.gameplay_last_heal_server_tick = server_tick
        CharacterService._send_player_heal_transition(
            s,
            current_health=current,
            maximum_health=maximum,
            recovered=recovered,
            server_tick=server_tick,
            label=label,
        )
        return recovered

    @staticmethod
    def _apply_player_kill_healing(
            s: Session, resolutions, *, label: str) -> int:
        """Restore the health that ``On:Kill`` talents grant per final blow."""

        kills = sum(
            1 for resolution in tuple(resolutions)
            if bool(getattr(resolution, "killed", False))
        )
        combat = getattr(s, "gameplay_combat_state", None)
        character = getattr(s, "gameplay_character", None)
        inventory = getattr(s, "gameplay_inventory", None)
        if kills <= 0 or combat is None or character is None or inventory is None:
            return 0
        path = combat.encounter.static_database_path
        programs = _kill_heal_programs(str(path))
        if not programs:
            return 0
        sources = CharacterService._active_skill_modifier_sources(s)
        hosted = [
            rule
            for source in sources
            for rule in programs.get(int(getattr(source, "effect_id", -1)), ())
        ]
        if not hosted:
            return 0
        from dso_server.gameplay.minion_auras import owner_actor_variable

        catalog = load_status_modifier_catalog(path)
        per_kill = 0.0
        for chance, amounts, bindings in hosted:
            if chance < 1.0:
                continue        # every authored kill heal is C:1.0
            values = [0.0] * (max((index for index, _ in bindings), default=-1) + 1)
            for index, binding in bindings:
                literal = resolve_modifier_value(binding)
                values[index] = (
                    literal if literal is not None
                    else owner_actor_variable(combat, sources, binding)
                )
            for amount in amounts:
                resolved = resolve_modifier_value(
                    amount, values=values, actor_variables=catalog.actor_variables,
                )
                per_kill += max(0.0, float(resolved or 0.0))
        recovered_request = int(round(per_kill * kills))
        if recovered_request <= 0:
            return 0
        actor_id = (
            (
                s.gameplay_remote_actor_id
                or CharacterService._remote_actor_id(character.character_id)
            )
            if s.gameplay_presence_callback is not None
            else combat.player_actor_id
        )
        with combat.lock:
            current, maximum, recovered = combat.heal_player_amount(
                actor_id, recovered_request
            )
        if recovered <= 0:
            return 0
        with s.flow_lock:
            inventory.health_points = float(current)
            server_tick = max(
                int(s.gameplay_chase_server_tick),
                int(s.gameplay_last_heal_server_tick) + 1,
            ) & 0xFFFFFFFF
            s.gameplay_last_heal_server_tick = server_tick
        CharacterService._send_player_heal_transition(
            s,
            current_health=current,
            maximum_health=maximum,
            recovered=recovered,
            server_tick=server_tick,
            label=label,
        )
        return recovered

    @staticmethod
    def _combat_commands_for_resolution(resolution):
        commands = []
        # Status-only skills (for example mage ``frostwind``) deliberately
        # resolve a target transaction with zero authored damage so their
        # victim effects can still be applied. A zero-valued Hit command is
        # not a harmless envelope: the client renders it as floating ``0``
        # combat text. Keep genuine zero-damage blocks, where the Hit flags
        # carry player-visible feedback, but let pure effect applications be
        # represented only by their StatusEffect commands below.
        if (
            int(getattr(resolution, "damage", 0)) > 0
            or bool(getattr(resolution, "blocked", False))
            or bool(getattr(resolution, "damage_immune", False))
            or bool(getattr(resolution, "killed", False))
        ):
            commands.append(HitServerCommand(
                Hit(
                    server_tick=resolution.server_tick,
                    hit_tags=resolution.hit_tags,
                    field_48=bool(getattr(resolution, "blocked", False)),
                    # +0x49 is CriticalHit in the client handler, not a
                    # killed flag. Death is represented by KillCommand.
                    field_49=bool(resolution.critical_hit),
                    current_health=resolution.current_health,
                    maximum_health=resolution.maximum_health,
                    absorbed_damage=0,
                    shield_damage=0,
                    damage=_wire_uint32(resolution.damage),
                    damage_type=3,
                    attacker_actor_id=resolution.attacker_actor_id,
                    source_actor_id=(
                        resolution.source_actor_id
                        if resolution.source_actor_id is not None
                        else resolution.attacker_actor_id
                    ),
                    skill_id=0,
                    impact_strength=0.0,
                    field_80=False,
                    field_81=False,
                    combat_text_type=_combat_text_type(
                        resolution.hit_tags,
                        periodic_damage=bool(resolution.periodic_damage),
                    ) if not bool(
                        getattr(resolution, "damage_immune", False)
                    ) else (
                        "DamageImmunityOther"
                    ),
                ),
                resolution.target_actor_id,
            ))
        if resolution.killed:
            commands.append(
                KillServerCommand(
                    Kill(
                        server_tick=resolution.server_tick,
                        hit_tags=resolution.hit_tags,
                        animation_time=getattr(resolution, "corpse_lifetime_ticks", 300),
                        field_4c=0,
                        killer_actor_id=resolution.attacker_actor_id,
                        position=resolution.position,
                        # Retail's lethal BulletSkill response uses zero here;
                        # the cast itself already identifies the skill index.
                        skill_id=0,
                        projectile_id=0xFFFFFFFF,
                        field_78=False,
                    ),
                    resolution.target_actor_id,
                )
            )
        return tuple(commands)

    @staticmethod
    def _combat_batch_for_resolution(resolution):
        """Compatibility wrapper for callers constructing one result."""

        return CombatBatch(
            CharacterService._combat_commands_for_resolution(resolution)
        )

    @staticmethod
    def _combat_batches_for_resolutions(
            resolutions, status_command_groups=None):
        """Pack retail Hit -> StatusEffect -> Kill transactions.

        A target transaction is indivisible even when it exceeds the ordinary
        command budget. RakNet may fragment that application message, but no
        unrelated reliable packet can be ordered between its hit result,
        effect reconciliation, and death transition.
        """

        resolutions = tuple(resolutions)
        if status_command_groups is None:
            status_command_groups = ((),) * len(resolutions)
        else:
            status_command_groups = tuple(status_command_groups)
        if len(status_command_groups) != len(resolutions):
            raise ValueError(
                "status command groups must align with combat resolutions"
            )
        batches = []
        buffered_commands = []
        for resolution, status_commands in zip(
                resolutions, status_command_groups):
            combat_commands = CharacterService._combat_commands_for_resolution(
                resolution
            )
            commands = (
                combat_commands[:1]
                + tuple(status_commands)
                + combat_commands[1:]
            )
            if (
                buffered_commands
                and len(buffered_commands) + len(commands)
                > COMBAT_BATCH_COMMAND_BUDGET
            ):
                batches.append(CombatBatch(tuple(buffered_commands)))
                buffered_commands = []
            buffered_commands.extend(commands)
        if buffered_commands:
            batches.append(CombatBatch(tuple(buffered_commands)))
        return tuple(batches)

    @staticmethod
    def _socketed_item_set_evaluation(s: Session):
        """The set evaluation with the zero-chance rules jewels enable.

        See ``item_set_procs.with_socket_overrides``: Jewel of Glory's
        ``mod`` rules must reach the set's program bookkeeping, not only the
        skill column.
        """

        return with_set_socket_overrides(
            getattr(s, "gameplay_item_set_evaluation", None),
            getattr(
                getattr(s, "gameplay_socket_effect_evaluation", None),
                "cast_rules", (),
            ),
        )

    @staticmethod
    def _item_set_procs_for_skill(s: Session, skill_index: int) -> dict:
        """Resolve this session's set procs for one cast skill, by target."""

        evaluation = CharacterService._socketed_item_set_evaluation(s)
        combat_state = getattr(s, "gameplay_combat_state", None)
        if evaluation is None or combat_state is None:
            return {}
        if not collect_set_proc_rules(evaluation):
            return {}
        database_path = getattr(
            getattr(combat_state, "encounter", None),
            "static_database_path",
            None,
        )
        if database_path is None:
            return {}
        geometry = combat_state._load_player_skill_geometry(int(skill_index))
        if geometry is None:
            return {}
        try:
            return resolve_set_procs_for_cast(
                evaluation,
                database_path,
                skill_id=geometry.resource_id,
                skill_groups=tuple(geometry.groups),
                loader=LocalCombatState._load_victim_status_effects,
                user_loader=_load_caster_status_effects,
                owner_loader=_load_caster_aura_owner_effects,
                column_effects=tuple(geometry.user_status_effects),
            )
        except Exception as exc:          # data-driven; never break combat
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] item-set proc resolution failed "
                f"for skill {skill_index}: {type(exc).__name__}: {exc}"
            )
            return {}

    @staticmethod
    def _item_set_trigger_effects(s: Session, trigger: str) -> tuple:
        """Resolve item-set effects fired by an authoritative event.

        Socket triggers have their own indexed dispatcher below.  Item-set
        programs share the combat event vocabulary and are resolved here from
        the active set evaluation instead of being limited to hit/kill.
        """

        evaluation = getattr(s, "gameplay_item_set_evaluation", None)
        combat_state = getattr(s, "gameplay_combat_state", None)
        if evaluation is None or combat_state is None:
            return ()
        database_path = getattr(
            getattr(combat_state, "encounter", None),
            "static_database_path",
            None,
        )
        if database_path is None:
            return ()
        try:
            from dso_server.services.item_set_events import active_item_set_evaluation
            evaluation = active_item_set_evaluation(s, evaluation, database_path)
            return resolve_set_trigger_effects(
                evaluation,
                database_path,
                trigger=trigger,
                loader=LocalCombatState._load_victim_status_effects,
            )
        except Exception as exc:          # data-driven; never break combat
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] item-set {trigger} trigger "
                f"resolution failed: {type(exc).__name__}: {exc}"
            )
            return ()

    @staticmethod
    def _item_set_user_proc_effects(
            s: Session, skill_index: int, *, triggers=("hit",)) -> tuple:
        """Caster-targeted (`addUE`/`modUE`) set procs for one cast.

        Rules split by authored trigger the same way the skill's own user
        column does: ``skillstart`` lands when the cast goes out, ``hit`` and
        the event triggers wait for a resolution. The impact path passes every
        trigger its batch satisfied at once so the rules are resolved once per
        skill rather than once per trigger.
        """

        wanted = {str(trigger).strip().casefold() for trigger in triggers}
        return tuple(
            definition
            for definition in CharacterService._item_set_procs_for_skill(
                s, skill_index
            ).get("user", ())
            if str(definition.trigger or "").casefold() in wanted
        )

    @staticmethod
    def _item_set_proc_effects(s: Session, resolutions) -> dict:
        """Item-set ``SkillStatusEffect`` procs for the skills being resolved.

        ``ItemSetEvaluation.skill_effect_rules`` was parsed but never read, so
        every "when you hit with X, apply Y" set bonus did nothing. Rolling the
        proc once per skill index -- rather than once per resolution -- keeps a
        multi-target cast from rolling the same bonus separately for every
        enemy it clips, which is what the authored chances assume.

        Returns ``{skill_index: (definition, ...)}`` for victim-targeted procs.
        """

        by_skill: dict[int, tuple] = {}
        for skill_index in {int(r.skill_index) for r in resolutions}:
            victim = CharacterService._item_set_procs_for_skill(
                s, skill_index
            ).get("victim", ())
            if victim:
                by_skill[skill_index] = victim
        return by_skill

    @staticmethod
    def _advance_status_effect_instance(
            s: Session, cache: dict, key: tuple[int, int], definition, *,
            actor_id: int, source_actor_id: int, server_tick: int,
            applied_at: float, position=None, yaw: float = 0.0):
        """Create one retail effect replacement and its prior-id cleanup.

        ``has_details`` selects construction in the reversed client handler.
        Consequently, sending another detailed record with the same instance
        id is a duplicate ADD, not a refresh. Retail allocates a fresh id for
        every application, then removes the superseded detailed instance with
        a no-details record. ``field_18`` is a monotonically increasing,
        zero-based occurrence for a running resource. It advances on every
        replacement, including resources whose authored ``MaxStackSize`` is
        one. Retail relies on that occurrence to distinguish the new detailed
        ADD from the old cleanup; resetting it to zero makes the client reject
        the replacement and the cleanup then removes the only visible effect.
        """

        duration = max(0, int(definition.duration_ticks))
        running = cache.get(key)
        previous = None
        if running is not None:
            previous, expires_at = running
            if float(expires_at) <= float(applied_at):
                cache.pop(key, None)
                previous = None

        stack_index = (
            int(previous.field_18) + 1
            if previous is not None else 0
        )
        causer_stack = str(getattr(definition, "effect_hosts", "")).casefold() == "causer"
        if causer_stack:
            # The combat state owns minion hit stacks; every viewer receives
            # the same occurrence, even after joining an already-hot minion.
            # ClientActorStatusEffectModule::AddEffect rejects explicit stack
            # indices >= MaxStackSize. Hit occurrences may keep increasing;
            # the authored minion stack index must remain bounded.
            stack_index = min(
                max(1, int(definition.max_stack_size)),
                max(1, int(getattr(definition, "stack_count", 1))),
            ) - 1
        instance_id = int(s.gameplay_next_status_effect_instance_id)
        s.gameplay_next_status_effect_instance_id += 1
        start_tick = int(server_tick) & 0xFFFFFFFF
        if causer_stack and previous is not None and stack_index == previous.field_18:
            # At the authored cap there is no new stack. Native no-details
            # field_25 refreshes the existing instance; another ADD followed
            # by cleanup can delete the capped effect when ADD is rejected.
            refreshed = replace(
                previous,
                field_08=start_tick,
                field_0c=(start_tick + duration) & 0xFFFFFFFF,
                field_10=duration,
                field_25=True,
                has_details=False,
                values=(),
            )
            cache[key] = (
                replace(refreshed, field_25=False, has_details=True, values=tuple(definition.values)),
                float(applied_at) + duration / float(SERVER_TICKS_PER_SECOND),
            )
            return refreshed, None, False
        has_aura_shape = bool(
            definition.aura_radius > 0.0
            and definition.aura_height > 0.0
            and position is not None
        )
        created = StatusEffectEntry(
            effect_id=int(definition.effect_id),
            field_04=instance_id,
            field_0c=(start_tick + duration) & 0xFFFFFFFF,
            field_14=SERVER_TICKS_PER_SECOND if duration else 0,
            field_08=start_tick,
            field_18=stack_index,
            field_10=duration,
            field_1c=100,
            field_20=int(source_actor_id),
            # A detailed record is the create branch. Retail leaves field_25
            # false in every captured detailed combat entry.
            field_25=False,
            # Retail marks every positive running-resource occurrence as a
            # counted replacement, including effects authored with
            # MaxStackSize=1.  Omitting this bit makes the client reject the
            # detailed replacement as already present; the following stale-id
            # cleanup then removes the only visible effect.
            field_26=bool(stack_index > 0),
            field_39=(definition.resource_id == "mercenary_ember_knight_overheated"),
            has_details=True,
            values=tuple(definition.values),
            field_38=False,
            field_3c=2 if has_aura_shape else 0xFF,
            shape_values=(
                (
                    float(definition.aura_radius),
                    float(definition.aura_height),
                    float(yaw),
                    *(float(value) for value in position),
                )
                if has_aura_shape else ()
            ),
        )
        removed = (
            replace(
                previous,
                field_0c=(start_tick + duration) & 0xFFFFFFFF,
                field_14=SERVER_TICKS_PER_SECOND if duration else 0,
                field_08=start_tick,
                field_10=duration,
                field_20=int(source_actor_id),
                field_25=False,
                field_39=False,
                has_details=False,
                values=(),
                field_38=False,
                field_3c=0xFF,
                shape_values=(),
            )
            if previous is not None else None
        )

        threshold = max(0, int(definition.max_stack_size) - 1)
        crossed_consuming_threshold = bool(
            getattr(definition, "consume_on_max_stack", False)
            and stack_index == threshold
        )
        if crossed_consuming_threshold:
            # The client's authored max-stack child stops this parent. The
            # newly-created edge instance is consumed client-side; retaining
            # it here would make the next server application continue a dead
            # generation rather than start again at zero.
            cache.pop(key, None)
        else:
            cache[key] = (
                created,
                float(applied_at)
                + duration / float(SERVER_TICKS_PER_SECOND),
            )
        return created, removed, crossed_consuming_threshold

    @staticmethod
    def _victim_effect_command_transactions(
            s: Session, resolutions, *, include_item_set_procs=True):
        """Build one status-command group for each damage resolution.

        Detailed creates are deliberately one-entry commands. Superseded ids
        are collected into a separate no-details removal command, matching all
        captured retail combat batches and preventing duplicate actor effects.
        """

        resolutions = tuple(resolutions)
        if VICTIM_STATUS_EFFECTS_DISABLED or STATUS_EFFECTS_DISABLED:
            # Bisection: Hit/Kill still resolve, only the effect commands that
            # drive client-side crowd control are withheld.
            return ((),) * len(resolutions)
        now = time.perf_counter()
        transactions = []
        combat = getattr(s, "gameplay_combat_state", None)
        path = getattr(getattr(combat, "encounter", None), "static_database_path", DEFAULT_STATIC_DB_PATH)
        cooldown_catalog = (
            load_status_modifier_catalog(path)
            if any(
                int(resolution.target_actor_id) == LOCAL_PLAYER_ACTOR_ID
                and any(
                    definition.modifier_kinds & {"activecooldown", "actorcooldownreset"}
                    for definition in resolution.victim_status_effects
                )
                for resolution in resolutions
            ) else None
        )
        with s.flow_lock:
            cache = s.gameplay_victim_status_effect_instances
            consumed_this_batch = set()
            # One cast advances a victim's stack once per effect, however many
            # of its impacts clip the same actor. The occurrence this sends is
            # what the client counts towards `MaxStackSize`, and it derives the
            # authored max-stack child from it locally -- for `debuff_cc_frost`
            # that child is the freeze, `ActorFeature:off,Movement,
            # RegularSkills`. Advancing per impact let a single 12-impact cast
            # push a monster twelve stacks closer to being disabled, which is
            # how ordinary spam reached a threshold meant for dedicated
            # stacking. This is the same "once per cast, not once per
            # resolution" rule the item-set procs already use.
            advanced_this_batch = set()
            cast_applications = getattr(s, 'gameplay_effect_cast_applications', None)
            if cast_applications is None:
                cast_applications = s.gameplay_effect_cast_applications = {}
            if len(cast_applications) > 16384:
                s.gameplay_effect_cast_applications = cast_applications = {
                    key: stamp for key, stamp in list(cast_applications.items())[-16384:]
                    if now - stamp < 120.0}
            for key, (_entry, expires_at) in list(cache.items()):
                if float(expires_at) <= now:
                    cache.pop(key, None)
            # A hit carries exactly the effects its caster resolved: the
            # caster's set rules are already in the skill's victim column
            # (item_set_procs.build_definitions).  Deriving procs here from
            # ``s`` re-rolled them and, for every other viewer of the hit,
            # added that viewer's own equipment.  ``include_item_set_procs``
            # is kept for callers and no longer adds anything.
            proc_effects = {}
            for resolution in resolutions:
                target_actor_id = int(resolution.target_actor_id)
                source_actor_id = int(
                    resolution.source_actor_id
                    if resolution.source_actor_id is not None
                    else resolution.attacker_actor_id
                )
                effect_source_actor_id = source_actor_id
                combat_state = getattr(s, "gameplay_combat_state", None)
                if (
                    combat_state is not None
                    and int(resolution.attacker_actor_id)
                    in getattr(combat_state, "player_summons", {})
                ):
                    # A mercenary's hit effects must name the mercenary as
                    # their causer so authored AuraTargets:Minion effects
                    # resolve to that actor. Keep the player as the damage,
                    # loot, and life-leech owner above.
                    effect_source_actor_id = int(
                        resolution.attacker_actor_id
                    )
                definitions = (
                    ()
                    if bool(getattr(resolution, "damage_immune", False))
                    else tuple(resolution.victim_status_effects) + tuple(
                        proc_effects.get(int(resolution.skill_index), ())
                    )
                )
                # ``EffectHosts:Causer`` is hosted by whoever caused the hit,
                # never by its victim (the minion path already keeps them on
                # the minion); the caster's own session applies them, see
                # _apply_caster_hosted_hit_effects.
                definitions = tuple(
                    definition for definition in definitions
                    if str(getattr(definition, "effect_hosts", "")).casefold() != "causer"
                )
                commands = []
                removals = []
                # Copies authored inside one application stack: Jewel of
                # Glory's explosion lists debuff_dot_burn three times ("3
                # stacks of Burning"). Only later impacts of the cast wait.
                advanced_here, stamped_here = set(), []
                for definition in definitions:
                    instant_cooldown = int(definition.duration_ticks) <= 0 and bool(
                        definition.modifier_kinds & {"activecooldown", "actorcooldownreset"}
                    )
                    if int(definition.duration_ticks) <= 0 and not instant_cooldown:
                        continue
                    key = (target_actor_id, int(definition.effect_id))
                    cast_id = int(getattr(resolution, 'cast_id', 0))
                    cast_key = (source_actor_id, cast_id, *key)
                    if cast_id and now - cast_applications.get(cast_key, float('-inf')) < 120.0:
                        continue
                    if key in consumed_this_batch:
                        continue
                    if key in advanced_this_batch:
                        # An earlier impact of this same cast already applied
                        # it to this actor. Re-sending would be a duplicate
                        # detailed ADD carrying an instance id the client
                        # already holds, which it rejects outright -- the
                        # "Effect already present!" storm in the client log.
                        continue
                    created, removed, consumed = (
                        CharacterService._advance_status_effect_instance(
                            s,
                            cache,
                            key,
                            definition,
                            actor_id=target_actor_id,
                            source_actor_id=effect_source_actor_id,
                            server_tick=int(resolution.server_tick),
                            applied_at=now,
                        )
                    )
                    if target_actor_id == LOCAL_PLAYER_ACTOR_ID and cooldown_catalog is not None:
                        apply_skill_cooldown_effect_locked(
                            s, created, path, now=now, catalog=cooldown_catalog,
                        )
                    if instant_cooldown:
                        cache.pop(key, None)
                    advanced_here.add(key)
                    if cast_id:
                        stamped_here.append(cast_key)
                    commands.append(
                        StatusEffectServerCommand(
                            StatusEffect(False, (created,)),
                            target_actor_id,
                        )
                    )
                    if removed is not None:
                        removals.append(removed)
                    if consumed:
                        consumed_this_batch.add(key)
                advanced_this_batch.update(advanced_here)
                for cast_key in stamped_here:
                    cast_applications[cast_key] = now
                if removals:
                    commands.append(
                        StatusEffectServerCommand(
                            StatusEffect(False, tuple(removals)),
                            target_actor_id,
                        )
                    )
                transactions.append(tuple(commands))
                if resolution.killed:
                    for key in tuple(cache):
                        if int(key[0]) == target_actor_id:
                            cache.pop(key, None)
        return tuple(transactions)

    @staticmethod
    def _send_combat_resolutions(
            s: Session, resolutions, *, label: str,
            packet_cache: dict | None = None,
            include_item_set_procs: bool = True) -> None:
        """Advance viewer state and send one ordered combat transaction.

        The packet cache is deliberately caller-owned and short lived.  A
        multiplayer presence event can map the same immutable resolutions to
        the same actor ids for many viewers.  Reusing those raw command
        payloads avoids repeating the bit-level encoder for every peer while
        each session still allocates its own RakNet sequence/order indexes and
        retains its own reliable backlog entry.
        """

        combat = getattr(s, "gameplay_combat_state", None)
        player_ids = getattr(combat, "player_health", {})
        # Kill's historically named animation_time is the corpse lifetime.
        # Retail keeps player bodies for 2,160,000 ticks, until Revive/map
        # teardown; the short monster lifetime fades their model away.
        resolutions = tuple(
            replace(resolution, corpse_lifetime_ticks=2_160_000)
            if resolution.killed and (
                int(resolution.target_actor_id) == LOCAL_PLAYER_ACTOR_ID
                or int(resolution.target_actor_id) in player_ids
            ) else resolution
            for resolution in resolutions
        )
        if not resolutions:
            return
        status_command_groups = (
            CharacterService._victim_effect_command_transactions(
                s, resolutions, include_item_set_procs=include_item_set_procs
            )
        )
        batches = CharacterService._combat_batches_for_resolutions(
            resolutions, status_command_groups
        )
        combat_cache_key = ("combat-transactions", batches)
        encoded_batches = (
            packet_cache.get(combat_cache_key)
            if packet_cache is not None
            else None
        )
        if encoded_batches is None:
            encoded_batches = tuple(
                encode_combat_batch(batch) for batch in batches
            )
            if packet_cache is not None:
                packet_cache[combat_cache_key] = encoded_batches
        for index, packet in enumerate(encoded_batches):
            s.send_reliable_ordered(
                packet,
                track=True,
                label=(
                    label
                    if len(encoded_batches) == 1
                    else (
                        f'{label}-{index + 1}-of-{len(encoded_batches)}'
                    )
                ),
            )

        if any(resolution.killed and int(resolution.target_actor_id) == LOCAL_PLAYER_ACTOR_ID
               for resolution in resolutions):
            CharacterService._notify_player_death(s)
        CharacterService._apply_caster_hosted_hit_effects(s, resolutions)
        CharacterService._apply_receiver_hosted_hit_auras(s, resolutions)

    @staticmethod
    def _apply_receiver_hosted_hit_auras(s: Session, resolutions) -> None:
        """Explode a hit's victim-hosted aura that has no duration.

        A ``D:0`` aura in a skill's victim column is never installed as a
        status effect (``_victim_effect_command_transactions`` skips zero
        durations): the client hosts it on the victim for one application
        after its ``DE`` delay and applies its ``AuraEffectId`` children to
        the actors around it. Jewel of Glory's Explosive Arrow explosion and
        the Marked armor shred (``ranger_explosive_shot_shredarmor_aura``)
        are such auras. Only the caster's own session resolves them, once.
        ``KeepOnDeath`` auras still go off when the hit was lethal.
        """

        character = s.gameplay_character
        combat = s.gameplay_combat_state
        if character is None or combat is None or STATUS_EFFECTS_DISABLED:
            return
        owner_actor_id = int(
            getattr(s, "gameplay_remote_actor_id", 0)
            or CharacterService._remote_actor_id(character.character_id)
        )
        own_ids = {LOCAL_PLAYER_ACTOR_ID, owner_actor_id}
        summons = getattr(combat, "player_summons", {})
        explosions = []
        for resolution in resolutions:
            source = (
                resolution.source_actor_id
                if resolution.source_actor_id is not None
                else resolution.attacker_actor_id
            )
            target = int(resolution.target_actor_id)
            if (
                int(source) not in own_ids
                or int(resolution.attacker_actor_id) in summons
                or target in own_ids
                or target not in getattr(combat, "monsters", {})
                or bool(getattr(resolution, "damage_immune", False))
            ):
                continue
            for definition in resolution.victim_status_effects:
                if (
                    str(getattr(definition, "effect_hosts", "")).casefold() == "causer"
                    or int(definition.duration_ticks) > 0
                    or float(getattr(definition, "aura_radius", 0.0)) <= 0.0
                    or float(definition.chance) <= 0.0
                    # Relative to the hosting monster: itself and its allies,
                    # the caster's enemies. An Enemies aura on a monster
                    # would strike players and is not a hit payoff.
                    or not {
                        value.strip().casefold()
                        for value in str(getattr(definition, "aura_targets", "")).split(";")
                    } & {"allies", "creator"}
                ):
                    continue
                if resolution.killed and not _keeps_on_death(
                        combat.encounter.static_database_path, definition.resource_id):
                    continue
                explosions.append((definition, target, resolution))
        for definition, target, resolution in explosions:
            delay = int(definition.delay_ticks) / float(SERVER_TICKS_PER_SECOND)
            arguments = (s, s.gameplay_chase_generation, combat, definition, target,
                         int(resolution.server_tick) + int(definition.delay_ticks),
                         int(resolution.skill_index), tuple(resolution.position))
            if delay <= 1e-6:
                CharacterService._explode_receiver_hosted_aura(*arguments)
                continue
            with s.flow_lock:
                key = ("receiver-hosted-aura", object())
                timer = s.gameplay_deadline_timer(
                    delay, CharacterService._explode_receiver_hosted_aura,
                    args=(*arguments, key))
                timer.daemon = True
                s.gameplay_projectile_timers[key] = timer
            timer.start()

    @staticmethod
    def _explode_receiver_hosted_aura(s: Session, generation, combat, definition,
                                      host_actor_id, server_tick, skill_index,
                                      fallback_position, timer_key=None) -> None:
        with s.flow_lock:
            if timer_key is not None and s.gameplay_projectile_timers.pop(timer_key, None) is None:
                return
            if (not s.connected or generation != s.gameplay_chase_generation
                    or s.gameplay_combat_state is not combat):
                return
            character = s.gameplay_character
        if character is None:
            return
        owner_actor_id = int(
            getattr(s, "gameplay_remote_actor_id", 0)
            or CharacterService._remote_actor_id(character.character_id)
        )
        from dso_server.persistence import readonly_db
        database = readonly_db.connect(combat.encounter.static_database_path)
        receivers = LocalCombatState._load_location_aura_effects(database, ",".join((
            definition.resource_id, "C:1.0",
            *(f"${index}:{value}" for index, value in enumerate(definition.values)),
        )))
        if not receivers:
            return
        from dso_server.services.endless_tower_buffs import tower_damage_range
        damage_range = tower_damage_range(
            combat.encounter.static_database_path, s,
            CharacterService._active_skill_modifier_sources(s),
        )
        with combat.lock:
            # Hosted on the victim, it goes off where the victim now stands.
            centre = tuple(combat.monster_positions.get(int(host_actor_id), fallback_position))
            resolutions = combat.resolve_receiver_hosted_aura(
                owner_actor_id, centre, definition, receivers,
                player_damage_range=damage_range,
                server_tick=int(server_tick), skill_index=int(skill_index),
            )
        # The host shows the aura for one tick so the client plays its
        # Done sequence (ranger_explosiveshot_explosion_bgh_jewel2026, the
        # blast itself); one tick is not zero, so it does not explode again.
        shown = replace(definition, duration_ticks=1, delay_ticks=0)
        resolutions = tuple(
            replace(value, victim_status_effects=(shown, *value.victim_status_effects))
            if int(value.target_actor_id) == int(host_actor_id) else value
            for value in resolutions
        )
        if resolutions:
            CharacterService._dispatch_player_owned_resolutions(
                combat, {owner_actor_id: s}, resolutions)

    @staticmethod
    def _apply_caster_hosted_hit_effects(s: Session, resolutions) -> None:
        """Host a hit's ``EffectHosts:Causer`` effects on the casting player.

        131 status rows are Causer-hosted; in skill victim columns they are
        the hitter's own on-hit buffs (``skill_mightystab_buff_attackspeed``,
        ``skill_frostwind_buff_iceball_damage``,
        ``SetLight2026_dwarf_SimpleShot_trig``).  Minion hits already keep
        them on the minion (LocalCombatState._apply_summon_causer_effects).
        Only the caster's own session applies them -- every viewer runs this
        transaction, but only one maps the source to itself -- once per
        effect per transaction, like the caster's other impact effects.  The
        skill column is their single roll; a set-named one (Quick Shot's
        Overload) runs as the set's program, see
        _send_player_skill_status_effects.
        """

        character = s.gameplay_character
        combat = s.gameplay_combat_state
        if character is None or STATUS_EFFECTS_DISABLED:
            return
        own_ids = {
            LOCAL_PLAYER_ACTOR_ID,
            int(getattr(s, "gameplay_remote_actor_id", 0)
                or CharacterService._remote_actor_id(character.character_id)),
        }
        summons = getattr(combat, "player_summons", {}) if combat is not None else {}
        hosted = {}
        server_tick = 0
        for resolution in resolutions:
            source = (
                resolution.source_actor_id
                if resolution.source_actor_id is not None
                else resolution.attacker_actor_id
            )
            if (
                int(source) not in own_ids
                or int(resolution.attacker_actor_id) in summons
                or int(resolution.target_actor_id) in own_ids
                or bool(getattr(resolution, "damage_immune", False))
            ):
                continue
            for definition in resolution.victim_status_effects:
                if (
                    str(getattr(definition, "effect_hosts", "")).casefold() != "causer"
                    or int(definition.duration_ticks) <= 0
                    or float(definition.chance) <= 0.0
                ):
                    continue
                hosted.setdefault(int(definition.effect_id), definition)
                server_tick = max(server_tick, int(resolution.server_tick))
        if hosted:
            CharacterService._send_player_skill_status_effects(
                s, tuple(hosted.values()), server_tick=server_tick,
                label="player-hit-causer-status-effects",
            )

    @staticmethod
    def _send_combat_resolution(
            s: Session, resolution, *, label: str,
            include_item_set_procs: bool = True) -> None:
        CharacterService._send_combat_resolutions(
            s, (resolution,), label=label,
            include_item_set_procs=include_item_set_procs,
        )

    @staticmethod
    def _running_player_status_effect(
            s: Session, effect_id: int, *, now: float):
        """Return ``(instance_id, entry)`` of a still-running player effect.

        Expired instances are dropped on the way, mirroring the sweep in
        ``_active_skill_modifier_sources``.
        """

        effect_id = int(effect_id)
        running = None
        for instance_id, (entry, expires_at) in list(
            s.gameplay_runtime_status_effects.items()
        ):
            if expires_at is not None and expires_at <= now:
                s.gameplay_runtime_status_effects.pop(instance_id, None)
                continue
            if (
                running is None
                and int(entry.effect_id) == effect_id
                and int(entry.field_20) == LOCAL_PLAYER_ACTOR_ID
            ):
                running = (instance_id, entry)
        return running

    @staticmethod
    def _send_attack_speed_bonus_stack(
            s: Session, *, attack_speed: float, server_tick: int,
            label: str) -> None:
        """Add the speed-tiered number of native damage stacks per cast."""

        speed = float(attack_speed)
        if speed + 1e-9 < 4.0:
            return
        stacks_per_cast = min(
            5,
            1 + math.floor((speed - 4.0 + 1e-9) / 0.5),
        )
        combat = s.gameplay_combat_state
        path = getattr(
            getattr(combat, "encounter", None),
            "static_database_path",
            DEFAULT_STATIC_DB_PATH,
        )
        stack, buff = _load_attack_speed_bonus_effects(str(path))
        CharacterService._send_player_skill_status_effects(
            s,
            (stack,) * stacks_per_cast,
            server_tick=server_tick,
            label=label,
            max_stack_followups={int(stack.effect_id): (buff,)},
            force_fresh_effect_ids={int(buff.effect_id)},
        )

    @staticmethod
    def _send_player_skill_status_effects(
            s: Session, definitions, *, server_tick: int,
            label: str, max_stack_followups=None,
            force_fresh_effect_ids=None, effect_position=None) -> None:
        if not definitions or STATUS_EFFECTS_DISABLED:
            return
        if getattr(s, "gameplay_item_set_programs", None):
            CharacterService._expire_runtime_status_effects(s)
        definitions = list(definitions)
        set_named = {
            rule.resource_id.casefold()
            for rule in collect_set_proc_rules(
                CharacterService._socketed_item_set_evaluation(s)
            )
            if rule.target in {"user", "victim"}
        }
        if set_named:
            # A set's SkillStatusEffect rule is delivered by the skill's own
            # column (item_set_procs.build_definitions); it still runs as the
            # set's program -- stack cap, immediate children, Done/Stop and
            # unequip cleanup.  Every authored target of such a rule is C:0.0
            # in its skill column, so only the set enables it.
            definitions = [
                replace(definition, item_set_program=True)
                if not getattr(definition, "item_set_program", True)
                and str(getattr(definition, "resource_id", "")).casefold() in set_named
                else definition
                for definition in definitions
            ]
        max_stack_followups = max_stack_followups or {}
        force_fresh_effect_ids = {
            int(effect_id) for effect_id in (force_fresh_effect_ids or ())
        }
        from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
        combat = s.gameplay_combat_state
        path = getattr(getattr(combat, 'encounter', None), 'static_database_path', DEFAULT_STATIC_DB_PATH)
        zero_duration_ids = load_status_runtime_catalog(path).zero_duration_effect_ids
        cooldown_catalog = load_status_modifier_catalog(path)
        with s.flow_lock:
            commands = []
            removals = []
            pre_removals = []
            consumed_this_batch = set()
            retired_timers = []
            periodic_effects = []
            minion_effects = []
            set_programs = []
            instant_programs = []
            stack_transitions = []
            applied_at = time.perf_counter()
            for definition in definitions:
                combat = s.gameplay_combat_state
                if (
                    combat is not None
                    and combat.player_status_minion_specs(definition)
                ):
                    minion_effects.append(definition)
                duration = int(definition.duration_ticks)
                instant_cooldown = duration <= 0 and bool(
                    definition.modifier_kinds & {"activecooldown", "actorcooldownreset"}
                )
                native_permanent = (duration <= 0 and definition.item_set_program
                                    and (definition.persistent or definition.item_set_parent_effect_id is not None))
                if duration <= 0 and not instant_cooldown and not native_permanent:
                    if definition.item_set_program:
                        # Nothing to install, but its Start program still
                        # runs (Jewel of Glory's warrior trigger).
                        instant_programs.append(definition)
                    continue
                if int(definition.effect_id) in consumed_this_batch:
                    continue
                start_tick = int(server_tick) & 0xFFFFFFFF
                running = CharacterService._running_player_status_effect(
                    s, definition.effect_id, now=applied_at
                )
                previous_id, previous = (
                    running if running is not None else (None, None)
                )
                if previous is not None and str(getattr(definition, "exclusivity_type", "")).casefold() == "deny":
                    continue
                instance_id = int(
                    s.gameplay_next_status_effect_instance_id
                )
                s.gameplay_next_status_effect_instance_id += 1
                # A max-stack followup such as the attack-speed damage buff
                # must restart as a fresh effect. Reusing a counted occurrence
                # makes the client rebuild it from the static template (10s),
                # ignoring the server's refreshed 30s duration override.
                stack_index = (
                    0
                    if int(definition.effect_id) in force_fresh_effect_ids
                    else (
                        int(previous.field_18) + 1
                        if previous is not None else 0
                    )
                )
                if definition.item_set_program:
                    stack_index = min(stack_index, max(0, definition.max_stack_size - 1))
                start_tick = int(server_tick) & 0xFFFFFFFF
                entry = StatusEffectEntry(
                        effect_id=int(definition.effect_id),
                        field_04=instance_id,
                        field_0c=0 if native_permanent else (start_tick + duration) & 0xFFFFFFFF,
                        field_14=25 if duration else 0,
                        field_08=start_tick,
                        field_18=stack_index,
                        field_10=duration,
                        field_1c=100,
                        field_20=LOCAL_PLAYER_ACTOR_ID,
                        field_25=False,
                        # This is an occurrence/replacement marker, not a
                        # "database allows stacks" marker.  Retail sets it for
                        # positive occurrences even when MaxStackSize is one.
                        field_26=bool(stack_index > 0),
                        # The native counted-player branch rebuilds from the
                        # static template and ignores the wire D override.
                        # Zero-duration templates must use its permanent bit;
                        # our existing timer still expires this runtime buff.
                        field_39=bool(native_permanent or (stack_index > 0 and definition.effect_id in zero_duration_ids)),
                        has_details=True,
                        values=definition.values,
                )
                capped_refresh = (
                    previous is not None
                    and definition.resource_id in {
                        "skill_frostwind_buff_iceball_damage",
                        "skill_frostwind_buff_frostnova_damage",
                    }
                    and previous.field_18 >= definition.max_stack_size - 1
                )
                if capped_refresh:
                    # At the cap the client rejects another counted ADD with
                    # the same index. Removing the old occurrence afterwards
                    # then erases the visible buff. Refresh its existing id.
                    instance_id = int(previous_id)
                    entry = replace(entry, field_04=instance_id,
                                    field_18=previous.field_18)
                wire_entry = (
                    replace(entry, field_25=True, has_details=False, values=())
                    if capped_refresh else entry
                )
                commands.append(
                    StatusEffectServerCommand(
                        StatusEffect(False, (wire_entry,)),
                        LOCAL_PLAYER_ACTOR_ID,
                    )
                )
                if previous is not None:
                    previous_removal = replace(
                        previous,
                        field_0c=(start_tick + duration) & 0xFFFFFFFF,
                        field_14=SERVER_TICKS_PER_SECOND,
                        field_08=start_tick,
                        field_10=duration,
                        field_20=LOCAL_PLAYER_ACTOR_ID,
                        field_25=False,
                        field_39=False,
                        has_details=False,
                        values=(),
                        field_38=False,
                        field_3c=0xFF,
                        shape_values=(),
                    )
                    if int(definition.effect_id) in force_fresh_effect_ids:
                        # Remove the still-active buff before adding its fresh
                        # 30-second instance at the max-stack boundary.
                        pre_removals.append(previous_removal)
                    elif not capped_refresh:
                        removals.append(previous_removal)
                    s.gameplay_runtime_status_effects.pop(
                        int(previous_id), None
                    )
                    old_timer = s.gameplay_projectile_timers.pop(
                        ("player-status-tick", int(previous_id)), None
                    )
                    if old_timer is not None:
                        retired_timers.append(old_timer)
                    expiry_timer = s.gameplay_projectile_timers.pop(
                        ('runtime-effect-expire', int(previous_id)), None)
                    if expiry_timer is not None:
                        retired_timers.append(expiry_timer)
                    program_timer = s.gameplay_projectile_timers.pop(
                        ('item-set-program-expire', int(previous_id)), None)
                    if program_timer is not None:
                        retired_timers.append(program_timer)
                if (
                    getattr(definition, "consume_on_max_stack", False)
                    and stack_index >= max(
                        0, int(definition.max_stack_size) - 1
                    )
                ):
                    # Clear the cap occurrence explicitly as well as resetting
                    # the server cache. The authored max-stack child may consume
                    # it locally, but clients can otherwise retain the counted
                    # occurrence and display 105, 110, ... across generations.
                    removals.append(replace(
                        entry,
                        has_details=False,
                        values=(),
                        field_25=False,
                        field_39=False,
                        field_38=False,
                        field_3c=0xFF,
                        shape_values=(),
                    ))
                    consumed_this_batch.add(int(definition.effect_id))
                    definitions.extend(
                        max_stack_followups.get(
                            int(definition.effect_id), ()
                        )
                    )
                elif not instant_cooldown:
                    s.gameplay_runtime_status_effects[instance_id] = (
                        entry,
                        None if native_permanent else applied_at
                        + duration / float(SERVER_TICKS_PER_SECOND),
                    )
                # Instant reset programs have no persistent status instance,
                # but still execute once and reach the client's cooldown UI.
                apply_skill_cooldown_effect_locked(
                    s, entry, path, now=applied_at, catalog=cooldown_catalog,
                )
                if not instant_cooldown and duration > 0:
                    periodic_effects.append((instance_id, definition))
                if definition.item_set_program:
                    set_programs.append((instance_id, definition))
                stack_transitions.append((definition,
                    min(int(previous.field_18) + 1, definition.max_stack_size) if previous else 0,
                    min(stack_index + 1, definition.max_stack_size)))
            if removals:
                commands.append(
                    StatusEffectServerCommand(
                        StatusEffect(False, tuple(removals)),
                        LOCAL_PLAYER_ACTOR_ID,
                    )
                )
        for timer in retired_timers:
            timer.cancel()
        output_error = None

        def finish_effect_step(operation, *args, **kwargs):
            # Set roots, children, vitals, and timers were accepted together.
            # A failed send must not leave only part of that state installed.
            nonlocal output_error
            try:
                return operation(*args, **kwargs)
            except OSError as error:
                if not set_programs:
                    raise
                output_error = output_error or error

        if set_programs:
            from dso_server.services.item_set_programs import accepted_item_set_programs
            finish_effect_step(accepted_item_set_programs, s, set_programs, server_tick=server_tick)
        if instant_programs:
            from dso_server.services.item_set_programs import run_instant_item_set_programs
            finish_effect_step(run_instant_item_set_programs, s, tuple(instant_programs),
                               server_tick=server_tick)
        if stack_transitions:
            from dso_server.services.item_set_events import dispatch_item_set_max_stack
            for definition, before, after in stack_transitions:
                finish_effect_step(dispatch_item_set_max_stack, s, definition, before, after, server_tick=server_tick)
        if commands:
            if pre_removals:
                commands.insert(
                    0,
                    StatusEffectServerCommand(
                        StatusEffect(False, tuple(pre_removals)),
                        LOCAL_PLAYER_ACTOR_ID,
                    ),
                )
            finish_effect_step(s.send_reliable_ordered,
                encode_status_effect_command_batch(
                    StatusEffectCommandBatch(tuple(commands))
                ),
                track=True,
                label=label,
            )
            if s.gameplay_presence_callback is not None:
                finish_effect_step(s.gameplay_presence_callback,
                    s,
                    (
                        "status_effects",
                        tuple(command.value for command in commands),
                    ),
                )
            vitality_kinds = {
                "healthpoints",
                "itemhealthpoints",
                "maxhealthpoints",
                "maxskillresource",
                "healthpointsregeneration",
                "skillresourceregeneration",
            }
            if any(
                definition.item_set_program or definition.modifier_kinds & vitality_kinds
                for definition in definitions
            ):
                finish_effect_step(CharacterService._refresh_player_combat_vitals, s, inventory_changed=False)
            finish_effect_step(CharacterService._apply_player_status_resource,
                s, definitions
            )
        for instance_id, definition in periodic_effects:
            runtime = s.gameplay_runtime_status_effects.get(instance_id)
            if runtime is not None and runtime[0].field_39 and not definition.item_set_program:
                CharacterService._schedule_counted_status_expiry(s, runtime[0], definition.duration_ticks)
            finish_effect_step(CharacterService._schedule_player_status_ticks,
                s,
                instance_id=instance_id,
                definition=definition,
            )
        for definition in minion_effects:
            finish_effect_step(CharacterService._spawn_socket_status_minions,
                s,
                definition,
                server_tick=server_tick,
                position_override=effect_position,
            )
        if commands:
            finish_effect_step(CharacterService._activate_player_user_effect_children,
                s, tuple((instance_id, definition) for instance_id, definition in periodic_effects),
                server_tick=server_tick, effect_position=effect_position,
            )
        if output_error is not None:
            raise output_error

    @staticmethod
    def _schedule_counted_status_expiry(s, entry, duration_ticks):
        # Counted effects authored with duration zero are client-permanent;
        # send an explicit removal at the finite server-owned deadline.
        with s.flow_lock:
            key = ('runtime-effect-expire', entry.field_04)
            timer = s.gameplay_deadline_timer(
                duration_ticks / float(SERVER_TICKS_PER_SECOND),
                CharacterService._expire_counted_status,
                args=(s, s.gameplay_chase_generation, entry))
            s.gameplay_projectile_timers[key] = timer
        timer.start()

    @staticmethod
    def _expire_counted_status(s, generation, entry):
        with s.flow_lock:
            s.gameplay_projectile_timers.pop(('runtime-effect-expire', entry.field_04), None)
            if not s.connected or generation != s.gameplay_chase_generation:
                return
            s.gameplay_runtime_status_effects.pop(entry.field_04, None)
            removal = CharacterService._consumable_cleanup_entry(entry, server_tick=s.gameplay_chase_server_tick)
        value = StatusEffect(False, (removal,))
        s.send_reliable_ordered(encode_status_effect_batch(value, actor_id=LOCAL_PLAYER_ACTOR_ID),
                               track=True, label='runtime-counted-status-expired')
        if s.gameplay_presence_callback:
            s.gameplay_presence_callback(s, ('status_effects', (value,)))
        CharacterService._refresh_player_combat_vitals(s, inventory_changed=False)

    @staticmethod
    def _schedule_player_status_ticks(
            s: Session, *, instance_id: int, definition) -> None:
        """Schedule authored health or cooldown ticks on the accepted effect."""

        with s.flow_lock:
            combat = s.gameplay_combat_state
            character = s.gameplay_character
            if combat is None or character is None:
                return
            generation = s.gameplay_chase_generation

        # Never wait for the shared simulation lock while retaining a session
        # flow lock.  The monster worker uses the opposite order for its
        # authoritative map mutation, so the old nesting could complete an
        # AB-BA deadlock with a peer's status-effect deadline.
        with combat.lock:
            program = combat.player_status_heal_program(definition)
            if program is None:
                cooldown_interval, tick_modifiers, _start = (
                    combat._status_effect_runtime_program(definition.effect_id)
                )
        if program is None:
            kinds = {
                raw.partition(":")[0].strip().casefold()
                for raw in tick_modifiers.split(";")
            }
            if not kinds & {"activecooldown", "actorcooldownreset"}:
                return
            interval = max(1.0 / SERVER_TICKS_PER_SECOND, cooldown_interval)
            duration = max(0, definition.duration_ticks) / SERVER_TICKS_PER_SECOND
            count = int(math.floor(duration / interval + 1e-6))
            if count <= 0:
                return
            program = (interval, 0.0, count, definition.delay_ticks / SERVER_TICKS_PER_SECOND)
        interval, fraction, count, delay = program

        with s.flow_lock:
            if (
                not s.connected
                or s.gameplay_combat_state is not combat
                or s.gameplay_character is not character
                or s.gameplay_chase_generation != generation
            ):
                return
            timer_key = ("player-status-tick", int(instance_id))
            old_timer = s.gameplay_projectile_timers.pop(timer_key, None)
            timer = s.gameplay_deadline_timer(
                max(0.0, delay + interval),
                CharacterService._tick_player_status_effect,
                args=(
                    s,
                    generation,
                    timer_key,
                    int(instance_id),
                    float(interval),
                    float(fraction),
                    int(count),
                ),
            )
            timer.daemon = True
            s.gameplay_projectile_timers[timer_key] = timer
        if old_timer is not None:
            old_timer.cancel()
        timer.start()

    @staticmethod
    def _tick_player_status_effect(
            s: Session, generation: int, timer_key,
            instance_id: int, interval: float,
            fraction: float, remaining_ticks: int) -> None:
        replacement = None
        healing_transition = None
        cooldown_catalog = None
        with s.flow_lock:
            combat = s.gameplay_combat_state
            character = s.gameplay_character
            runtime = s.gameplay_runtime_status_effects.get(instance_id)
            if (
                not s.connected
                or generation != s.gameplay_chase_generation
                or combat is None
                or character is None
                or runtime is None
                or (runtime[1] is not None and time.perf_counter() > runtime[1] + 0.05)
            ):
                s.gameplay_projectile_timers.pop(timer_key, None)
                return
            actor_id = (
                s.gameplay_remote_actor_id
                or CharacterService._remote_actor_id(character.character_id)
            )
        path = getattr(getattr(combat, "encounter", None), "static_database_path", DEFAULT_STATIC_DB_PATH)
        if getattr(runtime[0], "effect_id", None) is not None:
            cooldown_catalog = load_status_modifier_catalog(path)

        # Keep the shared combat mutation disjoint from the session flow
        # lock. The monster simulation takes those locks as one critical
        # section, and the former flow -> combat nesting here was the second
        # participant in the live AB-BA deadlock.
        previous = current = maximum = 0
        if fraction:
            with combat.lock:
                previous = combat.player_health.get(
                    actor_id,
                    [combat.player_maximum_health],
                )[0]
                current, maximum = combat.heal_player_relative(
                    actor_id, fraction
                )

        with s.flow_lock:
            runtime = s.gameplay_runtime_status_effects.get(instance_id)
            if (
                not s.connected
                or generation != s.gameplay_chase_generation
                or s.gameplay_combat_state is not combat
                or s.gameplay_character is not character
                or runtime is None
                or (runtime[1] is not None and time.perf_counter() > runtime[1] + 0.05)
            ):
                s.gameplay_projectile_timers.pop(timer_key, None)
                return
            if cooldown_catalog is not None:
                apply_skill_cooldown_effect_locked(
                    s, runtime[0], path, tick=True, catalog=cooldown_catalog,
                )
            recovered = int(current) - int(previous)
            if recovered > 0:
                next_tick = max(
                    int(s.gameplay_chase_server_tick),
                    int(s.gameplay_last_heal_server_tick) + 1,
                ) & 0xFFFFFFFF
                s.gameplay_last_heal_server_tick = next_tick
                if s.gameplay_inventory is not None:
                    s.gameplay_inventory.health_points = float(current)
                healing_transition = (
                    current, maximum, recovered, next_tick
                )
            remaining_ticks -= 1
            expires_at = runtime[1]
            if (
                remaining_ticks > 0
                and (
                    expires_at is None
                    or time.perf_counter() + interval <= expires_at + 0.05
                )
            ):
                replacement = s.gameplay_deadline_timer(
                    interval,
                    CharacterService._tick_player_status_effect,
                    args=(
                        s,
                        generation,
                        timer_key,
                        instance_id,
                        interval,
                        fraction,
                        remaining_ticks,
                    ),
                )
                replacement.daemon = True
                s.gameplay_projectile_timers[timer_key] = replacement
            else:
                s.gameplay_projectile_timers.pop(timer_key, None)
        if replacement is not None:
            replacement.start()
        if healing_transition is not None:
            current, maximum, recovered, tick = healing_transition
            CharacterService._send_player_heal_transition(
                s,
                current_health=current,
                maximum_health=maximum,
                recovered=recovered,
                server_tick=tick,
                label="player-status-heal",
            )

    @staticmethod
    def _send_entity_status_effects(
            s: Session, definitions, *, actor_id: int,
            server_tick: int, label: str,
            position: tuple[float, float, float] | None = None,
            yaw: float = 0.0) -> None:
        if not definitions or STATUS_EFFECTS_DISABLED:
            return
        with s.flow_lock:
            commands = []
            removals = []
            consumed_this_batch: set[tuple[int, int]] = set()
            applied_at = time.perf_counter()
            cache = s.gameplay_victim_status_effect_instances
            for definition in definitions:
                duration = int(definition.duration_ticks)
                if duration <= 0:
                    continue
                effect_id = int(definition.effect_id)
                key = (int(actor_id), effect_id)
                if key in consumed_this_batch:
                    continue
                created, removed, consumed = (
                    CharacterService._advance_status_effect_instance(
                        s,
                        cache,
                        key,
                        definition,
                        actor_id=int(actor_id),
                        source_actor_id=int(actor_id),
                        server_tick=int(server_tick),
                        applied_at=applied_at,
                        position=position,
                        yaw=float(yaw),
                    )
                )
                commands.append(
                    StatusEffectServerCommand(
                        StatusEffect(False, (created,)), int(actor_id)
                    )
                )
                if removed is not None:
                    removals.append(removed)
                if consumed:
                    consumed_this_batch.add(key)
            if removals:
                commands.append(
                    StatusEffectServerCommand(
                        StatusEffect(False, tuple(removals)), int(actor_id)
                    )
                )
        if not commands:
            return
        s.send_reliable_ordered(
            encode_status_effect_command_batch(
                StatusEffectCommandBatch(tuple(commands))
            ),
            track=True,
            label=label,
        )
