"""Run the event rules hosted by a character's combat talents.

The effect a Wisdom talent keeps on its owner authors the rule itself
(``trait_wisdom_ranger_killMarked_trigger_effect_trigger``:
``ActorStatusEffect:addAE,...,On:KillMarked``). Item sets and sockets have
their own dispatchers and nothing raised these events, so the talents could be
bought and did nothing. Strategic Advantage is not here: its
``SkillStatusEffect:addVE`` rule rides every cast and is decided by
``LocalCombatState._effect_applies``.

UNVERIFIED: not compared with a retail capture. The order of the Hit and its
status effects follows the server's other impact programs.
"""

from dataclasses import dataclass, field, replace
from functools import lru_cache

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.gameplay.status_modifier_runtime import (
    parse_modifier_program,
    resolve_modifier_value,
    snapshot_active_status_entries,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence import readonly_db
from dso_server.services.base import CharacterService
from dso_server.services.item_set_programs import wearer_children


# The events something below raises. ``Disabled`` means "you got stunned"
# (Master of Lightning's tooltip words the same trigger that way), not a rule
# switched off: LocalCombatState._effect_applies reads it as off for skill
# columns, which never reach this catalog.
TALENT_EVENTS = frozenset({
    "die", "disabled", "enterpvp", "hitblocked", "hitcritical", "hitlowhealth",
    "kill1hand", "kill2hand", "killmarked", "killplayer", "lowhealth", "nohealth",
})
# A rule held until ``OFF:`` has no duration of its own. A minute outlasts any
# of them: three percent a second lifts a player past a third of their health
# in far less, and the release ends it.
HELD_LEASE_TICKS = 60 * 25
# A turret talent's ground aura only carries a child onto the turret. These
# name what that child does: host an aura, or pay out on the turret's ``On:Death``.
MINION_SPAWNED = "minionspawned"
MINION_DESTROYED = "miniondestroyed"
HOSTED_MINION_TRIGGERS = {
    "immediatemanaged": MINION_SPAWNED,
    "death": MINION_DESTROYED,
}


@dataclass(frozen=True)
class TalentRule:
    event: str
    # "<effect>,C:1.0,D:2.0,...": one entry of a status effect column.
    entry: str
    # ExclusiveGroup of a Deny effect. One running member refuses the rule:
    # that is how the five-minute cooldown holds the talent back.
    deny_group: str
    # The host's own ``ActorVariable:0.75,absolute,HealOnDeath``, read here
    # because the health is given back under combat.lock.
    heal_on_death: float = 0.0
    # ``SkillStatusEffect:addUE``: the effect already rides each cast.
    rides_cast: bool = False
    # What the rule's skill scope summons. The authored aura lists are not
    # used: Compensation's names ``dwarf_*`` templates, and the Turret skills
    # summon ``minion_*``, so it would admit no turret a dwarf can build.
    templates: frozenset[str] = field(default_factory=frozenset)
    # ``addLE``: a ground aura at the owner, not an effect on them.
    location: bool = False
    # ``OFF:`` of a rule that lasts as long as its condition.
    released_by: str = ""
    # The host sits in ``DisabledMatchPvP``.
    off_in_match: bool = False


def _option(arguments, name: str) -> str:
    return next(
        (
            value.partition(":")[2].strip().casefold()
            for value in arguments
            if value.partition(":")[0].strip().casefold() == name
        ),
        "",
    )


def _trigger(arguments) -> str:
    return _option(arguments, "on")


def _scope_templates(database) -> dict[str, frozenset[str]]:
    """Each skill group's summoned monster templates."""

    scopes: dict[str, set[str]] = {}
    for groups, template in database.execute(
        "SELECT COALESCE(Groups, ''), SummonMonsterId FROM _Template_Skill "
        "WHERE COALESCE(SummonMonsterId, '') != ''"
    ):
        for group in str(groups).split(";"):
            if group.strip():
                scopes.setdefault(group.strip().casefold(), set()).add(
                    str(template).strip().casefold()
                )
    return {scope: frozenset(values) for scope, values in scopes.items()}


@lru_cache(maxsize=4)
def _talent_rules(database_path: str) -> dict[int, tuple[TalentRule, ...]]:
    """Map a talent effect's wire id to the event rules it hosts."""

    database = readonly_db.connect(database_path)
    rows = database.execute(
        "SELECT rowid - 1, Id, COALESCE(Groups, ''), "
        "COALESCE(StartModifiers, ''), COALESCE(ExclusiveGroup, ''), "
        "COALESCE(ExclusivityType, ''), COALESCE(AuraEffectId, '') "
        "FROM _Template_StatusEffect"
    ).fetchall()
    deny_groups = {
        str(row[1]).casefold(): str(row[4])
        for row in rows
        if str(row[5]).casefold() == "deny"
    }
    rows_by_id = {str(row[1]).casefold(): row for row in rows}
    scopes = _scope_templates(database)

    def hosted_on_minion(arguments):
        """Rules of an ``addLE`` ground aura's child on the turret."""

        aura = rows_by_id.get(arguments[2].strip().casefold())
        child = rows_by_id.get(str(aura[6]).strip().casefold()) if aura else None
        for instruction in parse_modifier_program(str(child[3]) if child else ""):
            hosted = instruction.arguments
            if instruction.kind != "actorstatuseffect" or len(hosted) < 2:
                continue
            event = HOSTED_MINION_TRIGGERS.get(_trigger(hosted[2:]))
            if event is not None:
                yield TalentRule(
                    event, ",".join(hosted[1:]), "",
                    templates=scopes.get(arguments[1].strip().casefold(), frozenset()),
                )

    def carried(effect):
        """A carrier's own rules: an ``EnterPvP`` state that only hosts one."""

        row = rows_by_id.get(effect.strip().casefold())
        for instruction in parse_modifier_program(str(row[3]) if row else ""):
            hosted = instruction.arguments
            if (
                instruction.kind == "actorstatuseffect" and len(hosted) >= 2
                and hosted[0].casefold() == "addae"
                and _trigger(hosted[2:]) in TALENT_EVENTS
            ):
                yield TalentRule(_trigger(hosted[2:]), ",".join(hosted[1:]), "")

    result = {}
    for wire_id, _resource, groups, program, _group, _exclusivity, _aura in rows:
        group_tokens = {
            value.strip().casefold() for value in str(groups).split(";")
        }
        if "talent" not in group_tokens:
            continue
        instructions = parse_modifier_program(str(program))
        heal_on_death = sum(
            resolve_modifier_value(instruction.arguments[0]) or 0.0
            for instruction in instructions
            if instruction.kind == "actorvariable"
            and len(instruction.arguments) >= 3
            and instruction.arguments[1].casefold() == "absolute"
            and instruction.arguments[2].strip().casefold() == "healondeath"
        )
        found = []
        for instruction in instructions:
            arguments = instruction.arguments
            operation = arguments[0].casefold() if arguments else ""
            if (
                instruction.kind == "actorstatuseffect"
                and operation in ("addae", "addle")
            ):
                effect, entry = 1, ",".join(arguments[1:])
            elif (
                instruction.kind == "skillstatuseffect"
                and operation == "addue" and len(arguments) >= 3
            ):
                effect, entry = 2, ",".join(arguments[2:])
            elif (
                instruction.kind == "skillstatuseffect"
                and operation == "addle" and len(arguments) >= 3
            ):
                found.extend(hosted_on_minion(arguments))
                continue
            else:
                continue
            event = _trigger(arguments[effect:])
            hosted = tuple(carried(arguments[effect])) if event == "enterpvp" else ()
            if hosted:
                found.extend(hosted)
            elif event in TALENT_EVENTS:
                found.append(TalentRule(
                    event, entry,
                    deny_groups.get(arguments[effect].strip().casefold(), ""),
                    float(heal_on_death),
                    rides_cast=effect == 2,
                    location=operation == "addle",
                    released_by=_option(arguments[effect:], "off"),
                    off_in_match="disabledmatchpvp" in group_tokens,
                ))
        if found:
            result[int(wire_id)] = tuple(found)
    return result


def _database_path(combat) -> str:
    return str(getattr(
        getattr(combat, "encounter", None),
        "static_database_path", DEFAULT_STATIC_DB_PATH,
    ))


def _live_sources(session) -> tuple:
    """The owner's running status sources, read without any lock.

    ``spare_player`` runs under combat.lock, where the map must never wait for
    one player's flow_lock. Each ``dict()`` is a single atomic copy, and the
    deadlines are enforced here because a timer can run late.
    """

    return snapshot_active_status_entries(
        dict(session.gameplay_persistent_status_effects),
        dict(session.gameplay_timed_status_effect_expiries),
        dict(session.gameplay_runtime_status_effects),
        dict(session.gameplay_item_set_effects),
        dict(session.gameplay_socket_effects),
    )


def _ready_rules(combat, sources, event: str) -> tuple[TalentRule, ...]:
    """The owner's rules for this event that no running effect denies."""

    rules = _talent_rules(_database_path(combat))
    return tuple(
        rule
        for source in sources
        for rule in rules.get(int(getattr(source, "effect_id", -1)), ())
        if rule.event == event
        and not (
            rule.deny_group
            and combat.status_sources_have_group(sources, (rule.deny_group,))
        )
    )


def _start_rules(session, combat, rules, event: str, *, server_tick: int,
                 count: int = 1) -> tuple:
    """Send each rule's effect with its ``On:Immediate`` children.

    ``count`` repeats what acts per application: "each final blow reduces your
    cool-downs", where a buff would only be refreshed.
    """

    database = readonly_db.connect(_database_path(combat))
    definitions = []
    placed = []
    for rule in rules:
        if rule.location:
            placed.extend(_place_ground_rule(
                session, combat, database, rule, server_tick=server_tick,
            ))
            continue
        for root in LocalCombatState._load_victim_status_effects(
            database, rule.entry, default_trigger=event,
        ):
            if rule.released_by and root.duration_ticks <= 0:
                root = replace(root, duration_ticks=HELD_LEASE_TICKS)
            definitions.append(root)
            definitions.extend(
                replace(child, item_set_program=False)
                for child in wearer_children(session, root)
            )
    repeated = tuple(
        definition for definition in definitions
        if "activecooldown" in definition.modifier_kinds
    ) * max(0, int(count) - 1)
    definitions = (*definitions, *repeated)
    CharacterService._send_player_skill_status_effects(
        session, definitions, server_tick=int(server_tick),
        label=f"talent-{event}",
        persistent_effect_ids=frozenset(
            int(definition.effect_id)
            for definition in definitions if definition.persistent
        ),
    )
    if definitions and getattr(session, "gameplay_pvp_enabled", False):
        position, _yaw = _owner_position(session, combat)
        damage_range = _owner_damage_range(session, combat)
        for aura, receivers in _ground_auras(combat, database, definitions):
            CharacterService._schedule_pvp_ground_strike(
                session, combat, aura, receivers, position=position,
                damage_range=damage_range, server_tick=int(server_tick),
                delay=aura.delay_ticks / 25.0,
            )
    return (*definitions, *placed)


def _ground_auras(combat, database, definitions) -> tuple:
    """The enemy auras these effects are or start, with their parameters.

    A death blast's ``addLE,mage_supernova_aura,...,$0:-10.0`` carries its
    damage in the rule. ``player_user_effect_location_auras`` loads the aura by
    name, which is enough for what it draws but strikes for nothing.
    """

    result = {}
    for definition in definitions:
        if (
            float(definition.aura_radius) > 0.0
            and definition.aura_targets.strip().casefold() == "enemies"
        ):
            encoded = (
                f"{definition.resource_id},C:1.0,"
                f"D:{definition.duration_ticks / 25.0}"
            )
            result[definition.resource_id] = (
                definition,
                LocalCombatState._load_location_aura_effects(database, encoded),
            )
        for kind, arguments in combat._status_modifier_program(
            int(definition.effect_id)
        ):
            if (
                kind.casefold() != "actorstatuseffect" or len(arguments) < 2
                or arguments[0].casefold() != "addle"
                or _trigger(arguments[2:]) not in ("", "immediate")
            ):
                continue
            encoded = ",".join(arguments[1:])
            for aura in LocalCombatState._load_victim_status_effects(
                database, encoded, default_trigger="immediate",
            ):
                result[aura.resource_id] = (
                    aura,
                    LocalCombatState._load_location_aura_effects(database, encoded),
                )
    return tuple(result.values())


def _owner_position(session, combat) -> tuple:
    move = session.gameplay_last_move
    if move is None:
        return tuple(session.gameplay_spawn_position), 0.0
    return (
        combat.encounter.coordinate_frame.wire_to_world(move.relative_position),
        float(getattr(move, "yaw", 0.0)),
    )


def _owner_damage_range(session, combat) -> tuple[float, float]:
    from dso_server.services.endless_tower_buffs import tower_damage_range

    return tower_damage_range(
        combat.encounter.static_database_path, session,
        CharacterService._active_skill_modifier_sources(session),
    )


def _place_ground_rule(session, combat, database, rule, *, server_tick: int) -> tuple:
    """An ``addLE`` rule: a ground aura at the owner, for monsters and players.

    Master of Lightning's field and Emergency Measures' stun. The visual and
    its monsters go through the location path; players it cannot reach.
    """

    receivers = LocalCombatState._load_location_aura_effects(database, rule.entry)
    auras = LocalCombatState._load_victim_status_effects(
        database, rule.entry, default_trigger="immediate",
    )
    if not auras:
        return ()
    position, yaw = _owner_position(session, combat)
    damage_range = _owner_damage_range(session, combat)
    owner_actor_id = int(
        session.gameplay_remote_actor_id
        or CharacterService._remote_actor_id(session.gameplay_character.character_id)
    )
    for aura in auras:
        delay = aura.delay_ticks / 25.0
        CharacterService._schedule_player_location_visuals(
            session, combat, (replace(aura, delay_ticks=0),),
            position=tuple(position), yaw=yaw,
            server_tick=int(server_tick) & 0xFFFFFFFF,
            owner_actor_id=owner_actor_id,
            cast_id=((int(server_tick) << 8) ^ int(aura.effect_id)) & 0xFFFFFFFF,
            delay_seconds=delay, receiver_status_effects=receivers,
            player_damage_range=damage_range,
        )
        CharacterService._schedule_pvp_ground_strike(
            session, combat, aura, receivers, position=position,
            damage_range=damage_range, server_tick=int(server_tick), delay=delay,
        )
    return auras


def dispatch_talent_event(session, event: str, *, server_tick: int,
                          count: int = 1) -> tuple:
    """Start what the owner's talents author for one committed event.

    Callers must not hold session or combat locks.
    """

    combat = getattr(session, "gameplay_combat_state", None)
    if combat is None:
        return ()
    in_match = getattr(session, "gameplay_match", None) is not None
    rules = tuple(
        rule for rule in _ready_rules(combat, _live_sources(session), event)
        if not (rule.off_in_match and in_match)
    )
    if not rules:
        return ()
    return _start_rules(
        session, combat, rules, event, server_tick=server_tick, count=count,
    )


def dispatch_player_death(session) -> tuple:
    """``On:Die``. The tooltips: "can only be accessed in an official arena"."""

    if getattr(session, "gameplay_match", None) is None:
        return ()
    return dispatch_talent_event(
        session, "die", server_tick=int(session.gameplay_chase_server_tick),
    )


def end_held_talent_effects(session, release: str) -> bool:
    """End what a talent held until ``OFF:<release>`` said otherwise."""

    combat = getattr(session, "gameplay_combat_state", None)
    if combat is None:
        return False
    ids = load_status_runtime_catalog(_database_path(combat)).effect_ids
    wanted = {
        ids.get(rule.entry.split(",", 1)[0].strip())
        for source in _live_sources(session)
        for rule in _talent_rules(_database_path(combat)).get(
            int(getattr(source, "effect_id", -1)), ()
        )
        if rule.released_by == release
    }
    if not wanted:
        return False
    return CharacterService._end_player_runtime_effects(
        session, lambda entry: int(entry.effect_id) in wanted,
        label=f"talent-{release}",
    )


def route_talent_user_effects(session, definitions, *, server_tick: int) -> tuple:
    """Take a talent's own effects out of a cast's user effects.

    Energetic Force's ``SkillStatusEffect:addUE`` rides each cast like a
    skill's own column, but what it starts is a carrier: the critical value
    buff and the cooldown are its ``On:Immediate`` children. Started plainly it
    showed nothing and ignored its cooldown. Every hosted rule rides a cast
    too: Emergency Reserves' mana came from the mage's own hits, where its
    tooltip says being hit, so a rule of ours is never sent from here. Returns
    the effects left to send.
    """

    definitions = tuple(definitions)
    combat = getattr(session, "gameplay_combat_state", None)
    if not definitions or combat is None:
        return definitions
    owned = {
        rule.entry.split(",", 1)[0].strip().casefold(): rule
        for rules in _talent_rules(_database_path(combat)).values()
        for rule in rules
    }
    events = dict.fromkeys(
        owned[definition.resource_id.casefold()].event
        for definition in definitions
        if definition.resource_id.casefold() in owned
        and owned[definition.resource_id.casefold()].rides_cast
    )
    for event in events:
        dispatch_talent_event(session, event, server_tick=server_tick)
    return tuple(
        definition for definition in definitions
        if definition.resource_id.casefold() not in owned
    )


def _weapon_kill_event(session) -> str:
    """``Kill1Hand`` or ``Kill2Hand``: slot 5 alone or both hand slots.

    The same test ``calculate_player_combat_stats`` uses for its weapon
    category. An independent offhand sits in slot 6 under its own item id.
    """

    inventory = getattr(session, "gameplay_inventory", None)
    for slots in tuple(getattr(inventory, "equipment_slots", {}).values()):
        if 5 in slots:
            return "kill2hand" if 6 in slots else "kill1hand"
    return ""


def dispatch_kill_talents(session, resolutions) -> tuple:
    """The final blows of one batch: ``KillMarked`` and the weapon kills.

    A killing blow installs nothing on its target, so a mark found here was
    there before the hit, not one it brought along.
    """

    kills = tuple(
        value for value in resolutions if getattr(value, "killed", False)
    )
    combat = getattr(session, "gameplay_combat_state", None)
    if not kills or combat is None:
        return ()
    sources = _live_sources(session)
    started = ()
    server_tick = max(int(value.server_tick) for value in kills)
    weapon_event = _weapon_kill_event(session)
    rules = _ready_rules(combat, sources, weapon_event) if weapon_event else ()
    if rules:
        started += _start_rules(
            session, combat, rules, weapon_event,
            server_tick=server_tick, count=len(kills),
        )
    rules = _ready_rules(combat, sources, "killmarked")
    if rules:
        with combat.lock:
            marked = any(
                getattr(value, "target_marked", False)
                or combat.monster_has_status_group(
                    value.target_actor_id, ("mark",)
                )
                for value in kills
            )
        if marked:
            started += _start_rules(
                session, combat, rules, "killmarked", server_tick=server_tick,
            )
    return started


def _stuns(combat, effects) -> bool:
    """Does a landed effect take away movement and regular skills?"""

    disabled = getattr(combat, "status_effect_disabled_features", None)
    return disabled is not None and any(
        {"movement", "regularskills"} <= disabled(int(effect.effect_id))
        for effect in effects
    )


def dispatch_incoming_hit(session, resolution) -> None:
    """The events one hit raises on the player who took it.

    Call once per hit after publishing it, without session or combat locks.
    A DoT tick is not a hit: "whenever you get hit" stays with real blows.
    """

    server_tick = int(resolution.server_tick)
    if getattr(resolution, "death_saved", False):
        dispatch_talent_event(session, "nohealth", server_tick=server_tick)
    if getattr(resolution, "killed", False):
        return
    if getattr(resolution, "blocked", False):
        dispatch_talent_event(session, "hitblocked", server_tick=server_tick)
    if getattr(resolution, "periodic_damage", False):
        return
    if int(getattr(resolution, "damage", 0)) > 0 and int(
        resolution.current_health
    ) <= max(1, round(int(resolution.maximum_health) / 3.0)):
        dispatch_talent_event(session, "hitlowhealth", server_tick=server_tick)
    effects = tuple(getattr(resolution, "victim_status_effects", ()))
    if effects and _stuns(getattr(session, "gameplay_combat_state", None), effects):
        dispatch_talent_event(session, "disabled", server_tick=server_tick)


def talent_minion_auras(session, summon) -> tuple:
    """The auras the owner's talents host on a turret that just spawned.

    Close to Turret reaches it through a 3 m ground aura whose child hosts the
    6 m ``AuraTargets:Summoner`` aura. Starting that aura at the spawn is the
    same result, and it goes through the summon-aura path that keeps a
    summoner's buff alive only while they stand inside. Its lease is the
    turret's: the authored 999 s would leave the ring on the ground.
    """

    combat = getattr(session, "gameplay_combat_state", None)
    if combat is None:
        return ()
    template = str(summon.definition.monster_template_id).casefold()
    rules = tuple(
        rule for rule in _ready_rules(combat, _live_sources(session), MINION_SPAWNED)
        if template in rule.templates
    )
    if not rules:
        return ()
    database = readonly_db.connect(_database_path(combat))
    expires_at = getattr(summon, "expires_at", None)
    lifetime = float(summon.definition.lifetime_seconds)
    result = []
    for rule in rules:
        receivers = LocalCombatState._load_location_aura_effects(database, rule.entry)
        for aura in LocalCombatState._load_victim_status_effects(
            database, rule.entry, default_trigger="immediate",
        ):
            if receivers and expires_at is not None and lifetime > 0.0:
                aura = replace(aura, duration_ticks=round(lifetime * 25))
            if receivers:
                result.append((aura, receivers))
    return tuple(result)


def dispatch_minion_destroyed(session, summon) -> tuple:
    """Compensation: an enemy destroyed one of the owner's turrets."""

    combat = getattr(session, "gameplay_combat_state", None)
    if combat is None:
        return ()
    template = str(summon.definition.monster_template_id).casefold()
    rules = tuple(
        rule
        for rule in _ready_rules(combat, _live_sources(session), MINION_DESTROYED)
        if template in rule.templates
    )
    if not rules:
        return ()
    return _start_rules(
        session, combat, rules, MINION_DESTROYED,
        server_tick=int(session.gameplay_chase_server_tick),
    )


def spare_player(session, combat, actor_id: int) -> bool:
    """Second Chance: give back the health a lethal blow just took.

    The talent hosts ``ActorVariable:0.75,absolute,HealOnDeath``, and the rule
    with its ``On:NoHealth`` is what separates it from the event arenas that
    set the same variable. Holding the save to the rule keeps those as they
    were. Callers hold combat.lock, have written the empty pool, and raise
    ``nohealth`` with ``dispatch_incoming_hit`` once the hit has been published.
    """

    actor_id = int(actor_id)
    health = combat.player_health.get(actor_id)
    if health is None or int(health[0]) > 0:
        return False
    fraction = max(
        (
            rule.heal_on_death
            for rule in _ready_rules(combat, _live_sources(session), "nohealth")
        ),
        default=0.0,
    )
    if fraction <= 0.0:
        return False
    health[0] = max(1, int(round(int(health[1]) * fraction)))
    if actor_id == int(combat.player_actor_id):
        combat.player_current_health = health[0]
    return True


def spare_lethal_hits(combat, targets, resolutions) -> tuple:
    """Rewrite the lethal hits a talent saves, before anyone is told of them.

    ``targets`` are the monster tick's (actor id, position, session) rows.
    Callers hold combat.lock.
    """

    resolutions = tuple(resolutions)
    if not any(value.killed for value in resolutions):
        return resolutions
    sessions = {int(actor_id): session for actor_id, _position, session in targets}
    result = []
    for value in resolutions:
        actor_id = int(value.target_actor_id)
        session = sessions.get(actor_id)
        if value.killed and session is not None and spare_player(
            session, combat, actor_id
        ):
            value = replace(
                value,
                killed=False,
                current_health=int(combat.player_health[actor_id][0]),
                death_saved=True,
            )
        result.append(value)
    return tuple(result)
