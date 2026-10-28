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
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

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


def with_socket_overrides(evaluation, socket_rules=()):
    """The set's ``SkillStatusEffect`` rules as the wearer's jewels leave them.

    A socketed jewel can enable a set rule that is authored with zero chance:
    Jewel of Glory's ``SkillStatusEffect:modUE,frostwind,
    jewel_bgh2026_mage_trigger,C:1.0`` turns on Forgotten Glory's
    ``addUE,frostwind,jewel_bgh2026_mage_trigger,C:0.0``. The skill column
    already merges the two (``_modified_player_skill_geometry``), so the
    effect is delivered; the set bookkeeping (program marking, Creator
    payloads) must see the same enabled rule, or the delivered effect runs as
    an inert shell. Only a ``mod`` of the same target, skill group and
    resource changes a rule, exactly as it changes the skill column.
    """

    modifiers = tuple(getattr(evaluation, "skill_effect_rules", ()) or ())
    overrides = []
    for rule in socket_rules or ():
        if str(getattr(rule, "kind", "")).casefold() != "skillstatuseffect":
            continue
        body = str(getattr(rule, "raw", "")).partition("#")[0]
        _kind, colon, argument_text = body.partition(":")
        arguments = tuple(part.strip() for part in argument_text.split(",")) if colon else ()
        if len(arguments) >= 3 and arguments[0].casefold().startswith("mod"):
            overrides.append(arguments)
    if not modifiers or not overrides:
        return evaluation
    changed = []
    for modifier in modifiers:
        arguments = tuple(modifier.arguments)
        merged = None
        for override in overrides:
            if (
                len(arguments) < 3
                or override[0].casefold()[3:] != arguments[0].casefold()[3:]
                or override[1].casefold() != arguments[1].casefold()
                or override[2].casefold() != arguments[2].casefold()
            ):
                continue
            options = {
                option.partition(":")[0].strip().casefold(): option.strip()
                for option in (merged or arguments)[3:] if option.strip()
            }
            options.update(
                (option.partition(":")[0].strip().casefold(), option.strip())
                for option in override[3:] if option.strip()
            )
            merged = (*arguments[:3], *options.values())
        changed.append(modifier if merged is None else replace(modifier, arguments=merged))
    try:
        return replace(evaluation, skill_effect_rules=tuple(changed))
    except TypeError:
        return SimpleNamespace(
            **{**vars(evaluation), "skill_effect_rules": tuple(changed)}
        )


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
    column_effects=(),
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

    A User/Victim rule itself is *not* returned.  ``add``/``mod<UE|VE>`` adds
    to or modifies the skill's own status column, and the item-set modifiers
    are in every caster's skill modifier sources, so
    ``_modified_player_skill_geometry`` already carries the rule in that column
    (every one of the 2,017 rule/skill pairs of the current data, with the
    rule's chance and the column's own trigger: ``angrystrike`` authors
    ``set_cny2026_warrior_earthquake_dmg`` ``ON:Hit``).  Returning it here
    applied it a second time with an independent roll -- two Earthquake stacks
    per Angry Strike, a 64% "30%" chain chance.  The skill is the one
    application and the one roll; delivery marks set-named resources as set
    programs (``CombatResolutionService._send_player_skill_status_effects``)
    and hosts ``EffectHosts:Causer`` ones on the caster.  Only the Creator
    payload, which no column carries, is resolved here.

    ``column_effects`` are the cast skill's own ``UserStatusEffects``. A
    ``modUE`` rule only changes the entry the column authors, so the shell
    lives for the column's ``D``, and so does a payload without a duration of
    its own: Forgotten Glory's ``modUE,TurretCombat,
    bgh_dwarf_bonus_heredur_dmg_aura,C:1.0`` enables the turret skill's 10 s
    shell, whose damage payload had expired with the row's 1 s default.
    """

    if not rules:
        return {}
    grouped: dict[str, list] = {}
    database = readonly_db.connect(database_path)
    columns = {
        str(effect.resource_id).casefold(): effect for effect in column_effects or ()
    }
    for rule in rules:
        encoded = rule.encoded
        column = columns.get(rule.resource_id.casefold())
        if column is not None and not any(
            option.partition(":")[0].strip().casefold() in {"d", "dp"}
            for option in rule.options
        ):
            durations = [f"D:{column.duration_ticks / 25.0:g}"]
            if getattr(column, "pvp_duration_ticks", None) is not None:
                durations.append(f"DP:{column.pvp_duration_ticks / 25.0:g}")
            encoded = ",".join((rule.resource_id, "C:1.0", *durations, *rule.options))
        if rule.target == "location":
            definitions = tuple(replace(d, item_set_program=True) for d in loader(database, encoded))
            if definitions:
                grouped.setdefault("location", []).extend(definitions)
            continue
        if rule.target != "user" or owner_loader is None:
            continue
        shells = (user_loader or loader)(database, encoded)
        if any(d.aura_targets.casefold() == "minion" for d in shells):
            # A Minion aura's Causer child is feedback that exists only while
            # an admitted minion is inside (services.minion_auras applies and
            # ends it); it is not a cast-time payload.
            continue
        # The skill applies the shell (it carries the aura shape the client
        # draws); its wearer payload follows the rule's own roll.  Every
        # authored Creator shell rule uses C:1.0, so the two cannot diverge.
        owned = tuple(replace(d, item_set_program=True) for d in owner_loader(database, encoded))
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
    operation: str = "addAE"
    options: tuple[str, ...] = ()
    source_set_id: str = ""

    @property
    def encoded(self) -> str:
        # Chance is rolled by the event dispatcher, exactly once. Preserve
        # authored duration, parameter, delay, and condition overrides.
        return ",".join((self.resource_id, "C:1.0", *self.options))

    @property
    def target(self) -> str:
        return "location" if self.operation.casefold().endswith("le") else "user"

    @property
    def off_trigger(self) -> str:
        return next((value.partition(":")[2].strip().casefold()
                     for value in self.options
                     if value.partition(":")[0].casefold() == "off"), "")


# Authoritative events emitted by combat, inventory pickup, and accepted
# status-stack transitions. ``Disabled`` deliberately remains unsupported.
SUPPORTED_TRIGGERS = {
    "kill", "hit", "pickuphealthglobe", "pickupingredientitem",
    "hitblocked", "lowhealth", "costskillresource",
    "maxstacksizeearthquake", "maxstacksizesimpleshot10",
    "maxstacksizesimpleshot20", "maxstacksizesimpleshot30",
}


def collect_trigger_rules(evaluation) -> tuple[SetTriggerRule, ...]:
    """Event-triggered actor rules from an ``ItemSetEvaluation``."""

    rules: list[SetTriggerRule] = []
    modifiers = tuple(getattr(evaluation, "actor_effect_rules", ()) or ())
    for modifier in modifiers:
        if not modifier.actor_operation.casefold().startswith("add"):
            continue
        # Later equipped tiers upgrade the original add rule. They commonly
        # supply only $1/$2, leaving its original trigger and duration intact.
        metadata = dict(modifier.metadata)
        for upgrade in modifiers:
            if (
                upgrade.actor_operation.casefold().startswith("mod")
                and upgrade.actor_effect_id.casefold()
                == modifier.actor_effect_id.casefold()
                and (
                    not upgrade.source_set_id
                    or not modifier.source_set_id
                    or upgrade.source_set_id == modifier.source_set_id
                )
            ):
                metadata.update(upgrade.metadata)
        trigger = metadata.get("on", "").strip().casefold()
        if trigger not in SUPPORTED_TRIGGERS:
            continue
        resource_id = modifier.actor_effect_id
        if not resource_id or resource_id == "empty_effect":
            continue
        try:
            chance = max(0.0, min(1.0, float(metadata.get("c", "1"))))
        except ValueError:
            chance = 0.0
        rules.append(
            SetTriggerRule(
                trigger=trigger,
                resource_id=resource_id,
                chance=chance,
                label=modifier.label,
                operation=modifier.actor_operation,
                options=tuple(f"{key}:{value}" for key, value in metadata.items()
                              if key != "c"),
                source_set_id=modifier.source_set_id,
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
        definitions.extend(
            replace(definition, item_set_program=True, item_set_target=rule.target)
            for definition in loader(database, rule.encoded)
        )
    # Native persistent roots (Mortis's counted variable contributions) and
    # authored D/DP overrides have distinct lifetimes. The runtime owns both.
    return tuple(definitions)


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
    column_effects=(),
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
        column_effects=column_effects,
    )
