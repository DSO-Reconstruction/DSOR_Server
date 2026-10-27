"""Save and restore earned native-persistent actor item-set occurrences.

The account codec already stores status entries and stable template names.
Keep counted occurrences there, but restore them into the runtime owner so
ordinary death/unequip cleanup and subsequent stacking remain authoritative.
"""

from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from dso_server.gameplay.item_set_procs import collect_trigger_rules
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.persistence import readonly_db
from dso_server.protocol.item_set_protocol import ItemSetDatabase
from dso_server.services.item_set_programs import children


def persistent_item_set_entries(session):
    """Copy immutable runtime entries while the caller holds flow_lock."""
    tracked = getattr(session, "gameplay_item_set_programs", {})
    return {
        instance_id: entry
        for instance_id, (entry, expiry) in session.gameplay_runtime_status_effects.items()
        if expiry is None and (definition := tracked.get(instance_id)) is not None
        and definition.item_set_program and definition.persistent
        and definition.duration_ticks <= 0 and definition.effect_id == entry.effect_id
    }


def _revision(path):
    path = Path(path).resolve()
    stat = path.stat()
    return str(path), stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=256)
def _eligible_definitions(path, _modified, _size, rules):
    database = readonly_db.connect(path)
    result = {}
    for rule in collect_trigger_rules(SimpleNamespace(actor_effect_rules=rules)):
        if rule.target != "user":
            continue
        for definition in LocalCombatState._load_victim_status_effects(database, rule.encoded):
            if not definition.persistent or definition.duration_ticks > 0:
                continue
            root = replace(definition, item_set_program=True, item_set_target="user")
            pending = [root]
            while pending:
                current = pending.pop()
                if current.effect_id in result:
                    continue
                result[current.effect_id] = current
                pending.extend(child for child in children(path, current)
                               if child.item_set_parent_effect_id is not None
                               and child.persistent and child.duration_ticks <= 0)
    return result


@lru_cache(maxsize=4)
def _known_effect_ids(path, modified, size):
    # Recognize saved sources even after the last supporting item was removed.
    # Source IDs prevent a tier upgrade from a different set changing a rule.
    rules = tuple(replace(modifier, source_set_id=definition.set_id)
                  for definition in ItemSetDatabase(path).definitions
                  for tier in definition.tiers for modifier in tier.modifiers
                  if modifier.actor_effect_id)
    return frozenset(_eligible_definitions(path, modified, size, rules))


def prepare_persistent_item_set_effects(state, database_path):
    """Prune ineligible saves before they affect initialization vitals/wire."""
    revision = _revision(database_path)
    rules = tuple(getattr(state.item_set_evaluation, "actor_effect_rules", ()))
    eligible = _eligible_definitions(*revision, rules)
    known = _known_effect_ids(*revision)
    selected = {}
    for instance_id, entry in sorted(state.persistent_status_effects.items()):
        if entry.effect_id not in known:
            continue
        definition = eligible.get(entry.effect_id)
        if definition is None or not entry.field_39 or entry.field_10 != 0:
            continue
        # One occurrence per effect, preserving its earned count rather than
        # replaying StartModifiers or firing the original combat event.
        selected[entry.effect_id] = replace(
            entry, field_08=0, field_0c=0,
            field_18=min(max(0, int(entry.field_18)), max(0, definition.max_stack_size - 1)),
        )
    for _ in range(16):
        orphans = {effect_id for effect_id in selected
                   if (parent := eligible[effect_id].item_set_parent_effect_id) is not None
                   and parent not in selected}
        if not orphans:
            break
        for effect_id in orphans:
            selected.pop(effect_id)
    state.persistent_status_effects = {
        instance_id: entry for instance_id, entry in state.persistent_status_effects.items()
        if entry.effect_id not in known
    }
    state.persistent_status_effects.update((entry.field_04, entry) for entry in selected.values())
    state.timed_status_effect_expiries = {
        instance_id: expiry for instance_id, expiry in state.timed_status_effect_expiries.items()
        if instance_id in state.persistent_status_effects
    }
    return {entry.field_04: eligible[effect_id] for effect_id, entry in selected.items()}


def restore_persistent_item_set_effects(session, state, database_path):
    """Restore tracked runtime occurrences without executing programs again."""
    definitions = prepare_persistent_item_set_effects(state, database_path)
    with session.flow_lock:
        # state keeps the wire initialization copy. The session must expose
        # each occurrence exactly once to active modifier-source evaluation.
        session.gameplay_persistent_status_effects = {
            instance_id: entry for instance_id, entry in state.persistent_status_effects.items()
            if instance_id not in definitions
        }
        session.gameplay_item_set_programs = definitions
        for instance_id in definitions:
            session.gameplay_runtime_status_effects[instance_id] = (
                state.persistent_status_effects[instance_id], None,
            )
        session.gameplay_disabled_feature_cache = None
