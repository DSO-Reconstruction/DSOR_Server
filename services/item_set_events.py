"""Execute item-set events raised by committed gameplay transitions."""

from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from dso_server.runtime import resident_content
from dso_server.gameplay.item_set_procs import collect_trigger_rules
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.status_modifier_runtime import (
    load_status_modifier_catalog, parse_modifier_program, resolve_modifier_value,
)
from dso_server.persistence import readonly_db
from dso_server.protocol.item_set_protocol import ItemSetModifier
from dso_server.services.base import CharacterService


# Hit/kill already run in impact resolution. Bridging only the new events
# avoids rolling those procs twice when the same impact also fires jewels.
BRIDGED_EVENTS = frozenset({
    "pickuphealthglobe", "pickupingredientitem", "hitblocked",
    "lowhealth", "costskillresource",
})

# These authored event names are aliases, not the full status resource ID.
MAX_STACK_EVENTS = {
    "set_cny2026_warrior_earthquake_dmg": "maxstacksizeearthquake",
    "setlight2026_dwarf_simpleshot_10": "maxstacksizesimpleshot10",
    "setlight2026_dwarf_simpleshot_20": "maxstacksizesimpleshot20",
    "setlight2026_dwarf_simpleshot_30": "maxstacksizesimpleshot30",
}


@lru_cache(maxsize=256)
def _equipment_actor_rules(path, _mtime, _size, templates):
    database = readonly_db.connect(path)
    result = []
    for template in templates:
        row = database.execute(
            "SELECT UniqueModifier, BaseModifier FROM _Template_Item WHERE Id=?",
            (template,),
        ).fetchone()
        for raw in row or ():
            for instruction in parse_modifier_program(raw):
                if instruction.kind.casefold() == "actorstatuseffect":
                    result.append(ItemSetModifier(
                        kind="ActorStatusEffect", arguments=instruction.arguments,
                        label=instruction.label, raw=instruction.raw,
                    ))
    return tuple(result)


def active_item_set_evaluation(session, evaluation, database_path):
    """Apply live modAE gates/parameters without mutating equipped rules.

    Temple's conditional parent and Black Dwarf's minion feedback enable
    otherwise zero-chance rules only while their authoring status is alive.
    """
    if evaluation is None or not evaluation.actor_effect_rules:
        return evaluation
    catalog = load_status_modifier_catalog(database_path)
    upgrades = []
    for entry in CharacterService._active_skill_modifier_sources(session):
        effect = catalog.definitions.get(int(getattr(entry, "effect_id", -1)))
        if effect is None:
            continue
        for instruction in effect.start_modifiers:
            arguments = instruction.arguments
            if (instruction.kind.casefold() != "actorstatuseffect"
                    or len(arguments) < 2
                    or not arguments[0].casefold().startswith("mod")):
                continue
            rewritten = list(arguments[:2])
            for argument in arguments[2:]:
                key, separator, value = argument.partition(":")
                if separator and value.startswith("$"):
                    number = resolve_modifier_value(
                        value, values=getattr(entry, "values", ()),
                        actor_variables=catalog.actor_variables,
                    )
                    if number is not None:
                        value = str(number)
                rewritten.append(f"{key}:{value}" if separator else argument)
            upgrades.append(ItemSetModifier(
                kind="ActorStatusEffect", arguments=tuple(rewritten),
                label=instruction.label, raw=instruction.raw,
            ))
    modified_resources = {
        rule.actor_effect_id.casefold()
        for rule in (*evaluation.actor_effect_rules, *upgrades)
        if rule.actor_operation.casefold().startswith("mod")
    }
    equipment_rules = ()
    templates = tuple(sorted(getattr(evaluation, "equipped_template_ids", ())))
    if modified_resources and templates:
        path = resident_content.resolved(database_path)
        stat = resident_content.stat(path)
        # Temple's selected torso provides the C:0 guardian rule; the set's
        # low-health parent enables it. Only join equipment rules explicitly
        # targeted by an active modAE, leaving unrelated item procs untouched.
        equipment_rules = tuple(
            rule for rule in _equipment_actor_rules(
                str(path), stat.st_mtime_ns, stat.st_size, templates,
            ) if rule.actor_effect_id.casefold() in modified_resources
            and rule.actor_operation.casefold().startswith("add")
        )
    if not upgrades and not equipment_rules:
        return evaluation
    return SimpleNamespace(actor_effect_rules=(
        *evaluation.actor_effect_rules, *equipment_rules, *upgrades,
    ))


def dispatch_item_set_event(session, event, *, server_tick, event_seed=0,
                            parent_definition=None):
    """Apply one accepted event; callers must not hold session/combat locks."""
    definitions = CharacterService._item_set_trigger_effects(session, event)
    if not definitions:
        return ()
    if parent_definition is not None:
        # CNY's zero-duration skill unlock is explicitly stopped by its
        # counted parent's Stop/Done program. Refreshing the parent at the
        # cap must not expire the unlock on an independent six-second lease.
        definitions = tuple(
            replace(value, item_set_parent_effect_id=int(parent_definition.effect_id))
            if value.duration_ticks <= 0
            and "unlockhiddenskillid" in value.modifier_kinds else value
            for value in definitions
        )
    users = tuple(value for value in definitions
                  if getattr(value, "item_set_target", "user") == "user")
    immediate = tuple(value for value in users if not value.delay_ticks)
    for definition in users:
        if definition.delay_ticks:
            with session.flow_lock:
                key = ("item-set-event-delay", object())
                timer = session.gameplay_deadline_timer(
                    definition.delay_ticks / 25.0, _apply_delayed_item_set_event,
                    args=(session, session.gameplay_chase_generation, key,
                          definition, int(server_tick)),
                )
                session.gameplay_projectile_timers[key] = timer
            timer.start()
    if immediate:
        CharacterService._send_player_skill_status_effects(
            session, immediate, server_tick=int(server_tick),
            label=f"item-set-{str(event).casefold()}",
        )
    locations = tuple(value for value in definitions
                      if getattr(value, "item_set_target", "user") == "location")
    if not locations:
        return definitions
    combat = session.gameplay_combat_state
    character = session.gameplay_character
    if combat is None or character is None:
        return definitions
    database_path = combat.encounter.static_database_path
    database = readonly_db.connect(database_path)
    move = session.gameplay_last_move
    position = (
        combat.encounter.coordinate_frame.wire_to_world(move.relative_position)
        if move is not None else tuple(session.gameplay_spawn_position)
    )
    actor_id = (session.gameplay_remote_actor_id
                or CharacterService._remote_actor_id(character.character_id))
    from dso_server.services.endless_tower_buffs import tower_damage_range
    damage = tower_damage_range(
        database_path, session,
        CharacterService._active_skill_modifier_sources(session),
    )
    for ordinal, definition in enumerate(locations):
        encoded = ",".join((definition.resource_id, "C:1.0",
                            f"D:{definition.duration_ticks / 25.0}",
                            *(f"${index}:{value}" for index, value
                              in enumerate(definition.values))))
        receivers = LocalCombatState._load_location_aura_effects(database, encoded)
        CharacterService._schedule_player_location_visuals(
            session, combat, (definition,), position=position,
            yaw=float(getattr(move, "yaw", 0.0)),
            server_tick=int(server_tick) & 0xFFFFFFFF,
            owner_actor_id=int(actor_id),
            cast_id=((int(event_seed) << 8) ^ ordinal ^ definition.effect_id)
                    & 0xFFFFFFFF,
            delay_seconds=definition.delay_ticks / 25.0,
            receiver_status_effects=receivers,
            player_damage_range=damage,
        )
    return definitions


def _apply_delayed_item_set_event(session, generation, key, definition, server_tick):
    with session.flow_lock:
        timer = session.gameplay_projectile_timers.pop(key, None)
        if (timer is None or not session.connected
                or generation != session.gameplay_chase_generation):
            return
        # Equipment can change during DE. A removed set cannot start a new
        # delayed program, while a live modAE gate need not roll a second time.
        if not any(rule.resource_id == definition.resource_id for rule
                   in collect_trigger_rules(session.gameplay_item_set_evaluation)):
            return
    CharacterService._send_player_skill_status_effects(
        session, (replace(definition, delay_ticks=0),),
        server_tick=(int(server_tick) + definition.delay_ticks) & 0xFFFFFFFF,
        label="item-set-delayed-event",
    )


def stop_item_set_condition(session, condition, *, server_tick):
    """Stop parents with the matching authored OFF condition, without a roll."""
    evaluation = getattr(session, "gameplay_item_set_evaluation", None)
    combat = session.gameplay_combat_state
    if evaluation is None or combat is None:
        return
    resources = {
        rule.resource_id for rule in collect_trigger_rules(evaluation)
        if rule.off_trigger == str(condition).casefold()
    }
    if not resources:
        return
    database = readonly_db.connect(combat.encounter.static_database_path)
    ids = {
        definition.effect_id for resource in resources
        for definition in LocalCombatState._load_victim_status_effects(database, resource)
    }
    CharacterService._end_player_runtime_effects(
        session, lambda entry: int(entry.effect_id) in ids,
        label=f"item-set-{str(condition).casefold()}-stop",
    )


def dispatch_item_set_max_stack(session, definition, previous_stack_count,
                                stack_count, *, server_tick):
    """Emit once when an accepted stack crosses its authored maximum."""
    cap = max(1, int(definition.max_stack_size))
    if not int(previous_stack_count) < cap <= int(stack_count):
        return ()
    event = MAX_STACK_EVENTS.get(str(definition.resource_id).casefold())
    if event is None:
        return ()
    return dispatch_item_set_event(
        session, event, server_tick=server_tick,
        event_seed=int(definition.effect_id), parent_definition=definition,
    )
