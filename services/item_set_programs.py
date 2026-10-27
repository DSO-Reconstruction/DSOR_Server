"""Lifetimes and managed children of accepted actor item-set programs."""

from functools import lru_cache
import sqlite3

from dso_server.runtime.context import *
from dso_server.services.base import CharacterService
from dso_server.gameplay.status_modifier_runtime import parse_modifier_program, resolve_modifier_value


def database_path(s):
    return getattr(getattr(s.gameplay_combat_state, "encounter", None),
                   "static_database_path", DEFAULT_STATIC_DB_PATH)


@lru_cache(maxsize=1024)
def _program(path, modified, size, effect_id):
    del modified, size
    with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT Id, StartModifiers, StopModifiers, DoneModifiers FROM _Template_StatusEffect WHERE rowid=?",
            (int(effect_id) + 1,),
        ).fetchone()
    return tuple(str(v or "") for v in row) if row else ("", "", "", "")


def program(path, effect_id):
    path = Path(path).resolve()
    stat = path.stat()
    return _program(str(path), stat.st_mtime_ns, stat.st_size, int(effect_id))


def children(path, definition):
    """Resolve immediate AE children; LE visuals use the existing aura path."""
    path = Path(path).resolve()
    stat = path.stat()
    return _children(str(path), stat.st_mtime_ns, stat.st_size, definition)


@lru_cache(maxsize=2048)
def _children(path, _modified, _size, definition):
    ancestry = (*definition.item_set_ancestry, int(definition.effect_id))
    if len(ancestry) >= 16:
        return ()
    result = []
    with sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True) as db:
        for instruction in parse_modifier_program(program(path, definition.effect_id)[1]):
            args = instruction.arguments
            if instruction.kind != "actorstatuseffect" or len(args) < 2 or args[0].casefold() != "addae":
                continue
            metadata = {k.casefold(): v for token in args[2:]
                        for k, sep, v in (token.partition(":"),) if sep}
            trigger = metadata.get("on", "immediate").casefold()
            if trigger not in {"immediate", "immediatemanaged"}:
                continue
            for child in LocalCombatState._load_victim_status_effects(db, ",".join(args[1:])):
                if child.effect_id in ancestry:
                    continue
                managed = trigger == "immediatemanaged"
                if child.duration_ticks <= 0 and managed:
                    child = replace(child, duration_ticks=definition.duration_ticks,
                                    persistent=definition.persistent)
                if child.duration_ticks <= 0 and not child.persistent:
                    continue
                result.append(replace(
                    child, item_set_program=True, item_set_target="user",
                    item_set_parent_effect_id=int(definition.effect_id) if managed else None,
                    item_set_ancestry=ancestry,
                ))
    return tuple(result)


def accepted_item_set_programs(s, applied, *, server_tick):
    """Run children and schedule terminal programs after releasing flow_lock."""
    path = database_path(s)
    accepted = []
    output_error = None
    for instance_id, definition in applied:
        if not definition.item_set_program:
            continue
        with s.flow_lock:
            if instance_id not in s.gameplay_runtime_status_effects:
                continue
            generation = s.gameplay_chase_generation
            tracked = getattr(s, "gameplay_item_set_programs", None)
            if tracked is None:
                tracked = s.gameplay_item_set_programs = {}
            # Replacing an occurrence refreshes its lease, rather than ending
            # the actor program and firing its StopModifiers.
            for old_id, old in tuple(tracked.items()):
                if old.effect_id == definition.effect_id:
                    tracked.pop(old_id, None)
            tracked[instance_id] = definition
            if definition.duration_ticks > 0:
                key = ("item-set-program-expire", int(instance_id))
                timer = s.gameplay_deadline_timer(
                    definition.duration_ticks / SERVER_TICKS_PER_SECOND,
                    _expire_item_set_program,
                    args=(s, generation, int(instance_id)),
                )
                s.gameplay_projectile_timers[key] = timer
            else:
                timer = None
        if timer is not None:
            timer.start()
        accepted.append((instance_id, definition, generation))
    # All accepted roots must remain tracked even if a child's transport fails.
    for instance_id, definition, generation in accepted:
        with s.flow_lock:
            if generation != s.gameplay_chase_generation or instance_id not in s.gameplay_runtime_status_effects:
                continue
        try:
            _terminal_programs(s, (definition,), server_tick=server_tick, reason="start",
                               expected_generation=generation)
        except OSError as error:
            output_error = output_error or error
        followups = []
        for child in children(path, definition):
            if child.chance < 1 and random.random() >= child.chance:
                continue
            if child.delay_ticks:
                with s.flow_lock:
                    if generation != s.gameplay_chase_generation:
                        continue
                    key = ("item-set-child", int(instance_id), int(child.effect_id))
                    timer = s.gameplay_deadline_timer(
                        child.delay_ticks / SERVER_TICKS_PER_SECOND, _apply_delayed_child,
                        args=(s, generation, key, child, server_tick),
                    )
                    s.gameplay_projectile_timers[key] = timer
                timer.start()
            else:
                followups.append(child)
        if followups and generation == s.gameplay_chase_generation:
            try:
                CharacterService._send_player_skill_status_effects(
                    s, followups, server_tick=server_tick, label="item-set-managed-effects")
            except OSError as error:
                output_error = output_error or error
    if accepted:
        # A set program can host a Minion aura or a live group-duration
        # modifier (Black Dwarf tiers 3-4): the owner's minions follow it now.
        try:
            _sync_minion_auras(s)
        except OSError as error:
            output_error = output_error or error
    if output_error is not None:
        raise output_error


def _sync_minion_auras(s):
    from dso_server.services.minion_auras import request_minion_aura_sync
    request_minion_aura_sync(s)


def _apply_delayed_child(s, generation, key, definition, server_tick):
    with s.flow_lock:
        if not s.connected or generation != s.gameplay_chase_generation:
            return
        timer = s.gameplay_projectile_timers.pop(key, None)
        parent = s.gameplay_runtime_status_effects.get(key[1])
        if timer is None or (definition.item_set_parent_effect_id is not None and (
                parent is None or (parent[1] is not None and parent[1] <= time.perf_counter()))):
            return
    CharacterService._send_player_skill_status_effects(
        s, (replace(definition, delay_ticks=0),),
        server_tick=(server_tick + definition.delay_ticks) & 0xFFFFFFFF,
        label="item-set-delayed-child")


def _expire_item_set_program(s, generation, instance_id):
    with s.flow_lock:
        if generation != s.gameplay_chase_generation or not s.connected:
            return
        definition = getattr(s, "gameplay_item_set_programs", {}).get(instance_id)
        if definition is None:
            return
    stop_item_set_effects(s, (definition.effect_id,), server_tick=s.gameplay_chase_server_tick,
                         reason="done", expected_instance_id=instance_id,
                         expected_generation=generation)


def stop_item_set_effects(s, effect_ids, *, server_tick, reason="stop", expected_instance_id=None,
                         expected_generation=None, _publication=None):
    """Remove selected effects and managed descendants, then execute cleanup."""
    wanted = {int(value) for value in effect_ids}
    root = _publication is None
    publication = [] if root else _publication
    removed, timers, ended = [], [], []
    with s.flow_lock:
        tracked = getattr(s, "gameplay_item_set_programs", {})
        if expected_generation is not None and expected_generation != s.gameplay_chase_generation:
            return
        generation = s.gameplay_chase_generation
        if expected_instance_id is not None and (
                expected_instance_id not in tracked
                or expected_instance_id not in s.gameplay_runtime_status_effects):
            return
        for _ in range(16):
            children_ids = {d.effect_id for d in tracked.values()
                            if d.item_set_parent_effect_id in wanted}
            if children_ids <= wanted:
                break
            wanted.update(children_ids)
        for instance_id, (entry, _expires) in tuple(s.gameplay_runtime_status_effects.items()):
            if int(entry.effect_id) not in wanted:
                continue
            s.gameplay_runtime_status_effects.pop(instance_id, None)
            definition = tracked.pop(instance_id, None)
            if definition is not None:
                ended.append(definition)
            removed.append(entry)
            # Unmanaged delayed starts can be authored exactly at parent end
            # (Black Dwarf overheat). Their callback retains its own generation
            # guard; managed children additionally require a live parent.
            for prefix in ("player-status-tick", "runtime-effect-expire", "item-set-program-expire"):
                timer = s.gameplay_projectile_timers.pop((prefix, int(instance_id)), None)
                if timer is not None:
                    timers.append(timer)
        if removed:
            s.gameplay_disabled_feature_cache = None
    for timer in timers:
        timer.cancel()
    if not removed:
        return
    # Mutate authoritative child/program state before any transport operation.
    _terminal_programs(s, ended, server_tick=server_tick, reason=reason,
                       _publication=publication, expected_generation=generation)
    value = StatusEffect(False, tuple(CharacterService._consumable_cleanup_entry(
        entry, server_tick=server_tick) for entry in removed))
    publication.append(("status", value))
    if root:
        if s.gameplay_chase_generation != generation:
            return
        CharacterService._refresh_player_combat_vitals(s, inventory_changed=False)
        try:
            _publish_program_changes(s, publication, generation=generation)
        finally:
            # An ended program can end a Minion aura or a live group-duration
            # bonus (Black Dwarf); the owner's minions follow it.
            _sync_minion_auras(s)


def _terminal_programs(s, definitions, *, server_tick, reason, _publication=None,
                       expected_generation=None):
    generation = s.gameplay_chase_generation if expected_generation is None else expected_generation
    if s.gameplay_chase_generation != generation:
        return
    path = database_path(s)
    root = _publication is None
    publication = [] if root else _publication
    stop_names, health_program = set(), []
    for definition in definitions:
        source = program(path, definition.effect_id)[1 if reason == "start" else 3 if reason == "done" else 2]
        for instruction in parse_modifier_program(source):
            if instruction.kind == "stopstatuseffect" and instruction.arguments:
                stop_names.add(instruction.arguments[0].split("|", 1)[0])
            elif instruction.kind == "currhealthpointsdmg":
                health_program.append((instruction, definition.values))
    stops = set()
    if stop_names:
        from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
        ids = load_status_runtime_catalog(path).effect_ids
        stops = {ids[name] for name in stop_names if name in ids}
    if stops:
        stop_item_set_effects(s, stops, server_tick=server_tick, _publication=publication,
                             expected_generation=generation)
    if health_program:
        _apply_terminal_health(s, health_program, server_tick=server_tick, publication=publication,
                               expected_generation=generation)
    if root and publication:
        if s.gameplay_chase_generation != generation:
            return
        CharacterService._refresh_player_combat_vitals(s, inventory_changed=False)
        _publish_program_changes(s, publication, generation=generation)


def _apply_terminal_health(s, instructions, *, server_tick, publication, expected_generation):
    with s.flow_lock:
        if s.gameplay_chase_generation != expected_generation:
            return
        combat, character = s.gameplay_combat_state, s.gameplay_character
        if combat is None or character is None or s.gameplay_death_pending:
            return
        actor_id = s.gameplay_remote_actor_id or CharacterService._remote_actor_id(character.character_id)
        match, match_callback = s.gameplay_match, s.gameplay_match_callback
    with combat.lock:
        if s.gameplay_chase_generation != expected_generation or s.gameplay_combat_state is not combat:
            return
        health = combat.player_health.get(actor_id)
        if health is None:
            return
        current, maximum = health
        for instruction, values in instructions:
            args = instruction.arguments
            value = resolve_modifier_value(args[0], values) if len(args) >= 2 else None
            if value is not None:
                current += value * maximum if args[1].casefold() == "relative" else value
        current = max(0, min(maximum, round(current)))
        health[0] = current
    with s.flow_lock:
        if s.gameplay_chase_generation != expected_generation or s.gameplay_combat_state is not combat:
            return
        if s.gameplay_inventory is not None:
            s.gameplay_inventory.health_points = current
            resource = s.gameplay_inventory.secondary_resource
        else:
            resource = 0
    publication.append(("health", ActorStatsUpdate(current, resource)))
    if current == 0:
        CharacterService._notify_player_death(s, expected_generation=expected_generation)
        with s.flow_lock:
            if s.gameplay_chase_generation != expected_generation or s.gameplay_combat_state is not combat:
                return
            same_match = s.gameplay_match is match and s.gameplay_match_callback is match_callback
        position = tuple(s.gameplay_last_move.relative_position) if s.gameplay_last_move else (0., 0., 0.)
        death = DamageResolution(
            target_actor_id=LOCAL_PLAYER_ACTOR_ID, attacker_actor_id=LOCAL_PLAYER_ACTOR_ID,
            current_health=0, maximum_health=int(maximum), damage=int(maximum), killed=True,
            position=position, server_tick=int(server_tick), hit_tags=(), skill_index=0,
            corpse_lifetime_ticks=2_160_000,
        )
        publication.append(("death", death))
        if same_match and match_callback is not None and match is not None:
            match_callback(s, ("match_kill", s))


def _publish_program_changes(s, publication, *, generation):
    for kind, value in publication:
        if s.gameplay_chase_generation != generation:
            return
        if kind == "status":
            packet = encode_status_effect_batch(value, actor_id=LOCAL_PLAYER_ACTOR_ID)
        elif kind == "health":
            packet = encode_actor_stats_update_batch(value, actor_id=LOCAL_PLAYER_ACTOR_ID)
        else:
            if s.gameplay_presence_callback is not None:
                actor_id = s.gameplay_remote_actor_id or CharacterService._remote_actor_id(s.gameplay_character.character_id)
                s.gameplay_presence_callback(s, ("pvp_combat_resolution", replace(
                    value, target_actor_id=actor_id, attacker_actor_id=actor_id,
                    source_actor_id=actor_id)))
                continue
            packet = encode_combat_batch(CombatBatch(CharacterService._combat_commands_for_resolution(value)))
        s.send_reliable_ordered(packet, track=True, label=f"item-set-program-{kind}")
        if kind == "status" and s.gameplay_presence_callback:
            s.gameplay_presence_callback(s, ("status_effects", (value,)))


def reconcile_item_set_programs(s):
    """A removed set/tier cannot leave its triggered programs running."""
    evaluation = getattr(s, "gameplay_item_set_evaluation", None)
    skill_roots = {str(rule.arguments[2]) for rule in getattr(evaluation, "skill_effect_rules", ())
                   if len(rule.arguments) >= 3}
    from dso_server.services.item_set_events import active_item_set_evaluation
    evaluation = active_item_set_evaluation(s, evaluation, database_path(s))
    roots = skill_roots | {str(rule.actor_effect_id) for rule in getattr(evaluation, "actor_effect_rules", ())}
    tracked = tuple(getattr(s, "gameplay_item_set_programs", {}).values())
    remove = {d.effect_id for d in tracked if d.item_set_parent_effect_id is None and d.resource_id not in roots}
    if remove:
        stop_item_set_effects(s, remove, server_tick=s.gameplay_chase_server_tick)
    # Equipping or removing a tier changes its ImmediateManaged Minion auras
    # even when no triggered program ends.
    _sync_minion_auras(s)
