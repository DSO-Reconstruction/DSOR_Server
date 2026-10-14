"""Resolve item-set ``SkillStatusEffect`` rules into applicable effects.

``_Template_ItemSet.Modifiers`` may carry rules of the form::

    SkillStatusEffect:addVE,<skillGroup>,<statusEffectId>,C:<chance>

meaning "when the wearer uses a skill belonging to ``<skillGroup>``, apply
``<statusEffectId>`` with probability ``<chance>``". ``ItemSetDatabase`` parses
these into ``ItemSetEvaluation.skill_effect_rules``, but nothing consumed that
field, so every proc-based set bonus was inert -- 118 modifiers across 13
entirely dead sets (``dso_server/docs/ITEM_SET_AUDIT.md``).

This module closes that gap. It matches rules against a cast skill and turns
them into the same ``VictimStatusEffectDefinition`` objects the combat pipeline
already applies for a skill's own ``VictimStatusEffects`` column, so the
downstream wire/stacking behaviour is shared rather than duplicated.

Operation keywords follow the client's vocabulary (see
``dso_server/docs/SET_CONCEPT_STORMBOUND.md`` §5):

    addVE / modVE -> the target        addUE / modUE -> the caster
    addLE         -> the location
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from dso_server.persistence import readonly_db

VICTIM_OPERATIONS = {"addve", "modve"}
USER_OPERATIONS = {"addue", "modue"}
LOCATION_OPERATIONS = {"addle"}


@dataclass(frozen=True)
class SetProcRule:
    """One ``SkillStatusEffect`` rule from an active item-set tier."""

    operation: str
    skill_group: str
    resource_id: str
    chance: float
    label: str = ""
    # Everything the rule authored after the effect id except ``C`` -- the
    # duration (``D``/``DP``), the detail parameters (``$N``) and the trigger
    # (``ON``). Dropping these made every rule inherit the effect row's own
    # duration and the parser's default trigger, so an ``ON:Kill`` bonus was
    # indistinguishable from an on-cast one.
    options: tuple[str, ...] = ()

    @property
    def target(self) -> str:
        operation = self.operation.casefold()
        if operation in VICTIM_OPERATIONS:
            return "victim"
        if operation in USER_OPERATIONS:
            return "user"
        if operation in LOCATION_OPERATIONS:
            return "location"
        return ""

    @property
    def encoded(self) -> str:
        """The rule as one status-effect column entry.

        ``C`` is pinned to 1.0 because the caller already rolled the authored
        chance; the shared parser resolves the rest from the effect row.
        """

        return ",".join((self.resource_id, "C:1.0", *self.options))


def collect_rules(evaluation) -> tuple[SetProcRule, ...]:
    """Extract usable proc rules from an ``ItemSetEvaluation``."""

    rules: list[SetProcRule] = []
    for modifier in getattr(evaluation, "skill_effect_rules", ()) or ():
        arguments = tuple(modifier.arguments)
        if len(arguments) < 3:
            continue
        operation, skill_group, resource_id = arguments[0:3]
        if not skill_group or not resource_id:
            continue
        if resource_id == "empty_effect":
            continue
        rule = SetProcRule(
            operation=operation.strip(),
            skill_group=skill_group.strip(),
            resource_id=resource_id.strip(),
            chance=float(modifier.chance),
            label=modifier.label,
            options=tuple(
                argument.strip()
                for argument in arguments[3:]
                if argument.strip()
                and argument.strip().partition(":")[0].casefold() != "c"
            ),
        )
        if rule.target and rule.chance > 0.0:
            rules.append(rule)
    return tuple(rules)


def rules_for_skill(
    rules: tuple[SetProcRule, ...],
    *,
    skill_id: str,
    skill_groups: tuple[str, ...],
) -> tuple[SetProcRule, ...]:
    """Rules whose group matches this skill.

    A skill's ``Groups`` column contains its own id (``chainlightning`` is in
    group ``chainlightning``), so a single comparison covers both
    "this exact skill" and "any skill in this family".
    """

    haystack = {group.casefold() for group in skill_groups if group}
    haystack.add(str(skill_id or "").casefold())
    return tuple(
        rule for rule in rules if rule.skill_group.casefold() in haystack
    )


def roll(rules: tuple[SetProcRule, ...], rng: random.Random | None = None):
    """Filter rules by their authored probability."""

    generator = rng or random
    return tuple(
        rule for rule in rules
        if rule.chance >= 1.0 or generator.random() < rule.chance
    )


def build_definitions(
    database_path: str | Path,
    rules: tuple[SetProcRule, ...],
    *,
    loader,
    user_loader=None,
    owner_loader=None,
) -> dict[str, tuple]:
    """Turn rules into ``VictimStatusEffectDefinition``s grouped by target.

    ``loader`` is ``LocalCombatState._load_victim_status_effects``; reusing it
    means duration, stack size and aura shape are resolved exactly the way a
    skill's own status-effect column is, instead of a second implementation
    that could drift.

    ``user_loader`` is the same parser with the caster's default trigger.  A
    skill's own ``UserStatusEffects`` default to ``skillstart``, and ``addUE``
    means "the caster", so a rule without an explicit ``ON:`` belongs on the
    cast rather than on an impact -- otherwise every set whose trigger skill
    deals no damage (Battle Cry, Adrenaline, Dwarf-in-the-Box) is inert.

    ``owner_loader`` is ``LocalCombatState._load_aura_owner_effects``.  Several
    sets name an ``AuraTargets:Creator`` shell as the caster effect; the buff
    the wearer is supposed to receive is that shell's ``AuraEffectId`` child,
    so applying the shell alone delivers a one-second empty aura and nothing
    else (``chillingly_cold_2025_aura_mage`` ->
    ``chillingly_cold_2025_trigger_mage``).
    """

    if not rules:
        return {}
    grouped: dict[str, list] = {}
    database = readonly_db.connect(database_path)
    for rule in rules:
        encoded = rule.encoded
        targets_user = rule.target == "user"
        resolve = user_loader if targets_user and user_loader else loader
        definitions = resolve(database, encoded)
        if definitions:
            grouped.setdefault(rule.target, []).extend(definitions)
        if targets_user and owner_loader is not None:
            # The shell is still applied above: it carries the aura shape the
            # client draws. Only its payload is missing.
            owned = owner_loader(database, encoded)
            if owned:
                grouped.setdefault("user", []).extend(owned)
    return {target: tuple(items) for target, items in grouped.items()}


@dataclass(frozen=True)
class SetTriggerRule:
    """An ``ActorStatusEffect`` rule fired by a combat event, not by equipping.

    ``ItemSetDatabase`` only applies triggers ``immediate``/``immediatemanaged``;
    everything else lands in ``ItemSetEvaluation.actor_effect_rules`` unread.
    """

    trigger: str
    resource_id: str
    chance: float
    label: str = ""


# Triggers this server can actually raise. The remainder of the shipped
# vocabulary needs systems that do not exist here: `pickuphealthglobe` (24
# modifiers) and `pickupingredientitem` have no pickup events,
# `hitblocked` no block resolution, `costskillresource` no resource-spend hook.
SUPPORTED_TRIGGERS = {"kill", "hit"}


def collect_trigger_rules(evaluation) -> tuple[SetTriggerRule, ...]:
    """Event-triggered actor rules from an ``ItemSetEvaluation``."""

    rules: list[SetTriggerRule] = []
    for modifier in getattr(evaluation, "actor_effect_rules", ()) or ():
        if not modifier.actor_operation.casefold().startswith("add"):
            # modAE edits an already-applied effect's parameters rather than
            # adding one; it is a tier-upgrade mechanic, not an event.
            continue
        trigger = modifier.trigger
        if trigger not in SUPPORTED_TRIGGERS:
            continue
        resource_id = modifier.actor_effect_id
        if not resource_id or resource_id == "empty_effect":
            continue
        chance = float(modifier.chance)
        if chance <= 0.0:
            continue
        rules.append(
            SetTriggerRule(
                trigger=trigger,
                resource_id=resource_id,
                chance=chance,
                label=modifier.label,
            )
        )
    return tuple(rules)


def resolve_for_trigger(
    evaluation,
    database_path: str | Path,
    *,
    trigger: str,
    loader,
    rng: random.Random | None = None,
) -> tuple:
    """Effects to apply to the wearer when ``trigger`` fires."""

    if evaluation is None:
        return ()
    wanted = str(trigger or "").casefold()
    rules = tuple(
        rule for rule in collect_trigger_rules(evaluation)
        if rule.trigger == wanted
    )
    if not rules:
        return ()
    generator = rng or random
    passed = tuple(
        rule for rule in rules
        if rule.chance >= 1.0 or generator.random() < rule.chance
    )
    if not passed:
        return ()
    definitions = []
    database = readonly_db.connect(database_path)
    for rule in passed:
        definitions.extend(loader(database, f"{rule.resource_id},C:1.0"))
    # 25 of the 42 shipped kill/hit effects declare StatusEffectDuration 0.
    # Those are not timed buffs -- they are instantaneous variable writes
    # (`item_pw_mortis_set_variable_damage_trigger` and friends) and need a
    # different, non-status-effect application path. Sending one as a timed
    # effect makes the client log "ignoring non-permanent effect with
    # total-duration 0", so drop them here rather than downstream, where it
    # would look like an accident.
    return tuple(
        definition for definition in definitions
        if int(definition.duration_ticks) > 0
    )


def resolve_for_cast(
    evaluation,
    database_path: str | Path,
    *,
    skill_id: str,
    skill_groups: tuple[str, ...],
    loader,
    user_loader=None,
    owner_loader=None,
    rng: random.Random | None = None,
) -> dict[str, tuple]:
    """Full path: evaluation + cast skill -> effects to apply, by target."""

    if evaluation is None:
        return {}
    rules = collect_rules(evaluation)
    if not rules:
        return {}
    matched = rules_for_skill(
        rules, skill_id=skill_id, skill_groups=skill_groups
    )
    if not matched:
        return {}
    return build_definitions(
        database_path,
        roll(matched, rng),
        loader=loader,
        user_loader=user_loader,
        owner_loader=owner_loader,
    )
