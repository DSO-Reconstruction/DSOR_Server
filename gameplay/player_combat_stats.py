"""Authoritative player damage derived from the static game database.

The client builds its displayed damage from several independent modifier
programs.  The server must use the same inputs when resolving a hit:

* class/level base damage from ``_Template_XPLevels``;
* equipped item, enchantment, socket, and weapon modifiers;
* active talent, pet, item-set, and runtime status effects; and
* PvE and weapon-specific damage factors.

All database-owned rows are loaded once per database revision.  Casts only
walk the small set of equipped items and active effects, so the combat loop
does not perform SQLite work.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import sqlite3
import threading
import time

from dso_server.runtime import resident_content
from dso_server.protocol.inventory_codec import active_equipment_slots
from dso_server.gameplay.item_random import deterministic_base_enchantment_rolls


@dataclass(frozen=True)
class CombatModifier:
    """One generated ``Kind:argument,...`` modifier instruction."""

    kind: str
    arguments: tuple[str, ...]
    source_set_id: str = ""


@dataclass(frozen=True)
class _EnchantmentDefinition:
    modifiers: tuple[CombatModifier, ...]
    scale_absolute: bool
    use_tier_factor: bool
    minimum_item_level: int
    maximum_item_level: int
    scale_reference_level: int

    def effective_level(self, item_level: int) -> int:
        level = max(self.minimum_item_level, int(item_level))
        if self.maximum_item_level > 0:
            level = min(level, self.maximum_item_level)
        return max(1, level)


@dataclass(frozen=True)
class _ItemDefinition:
    template_id: str
    category: str
    minimum_damage: float
    maximum_damage: float
    scale_with_level: bool
    base_enchantment_ids: tuple[str, ...]
    template_enchantment_ids: tuple[str, ...]
    modifiers: tuple[CombatModifier, ...]


@dataclass(frozen=True)
class _Instruction:
    modifier: CombatModifier
    stack_count: int = 1
    details: tuple[float, ...] = ()
    absolute_scale: float = 1.0
    scale_relative: bool = False
    amount_multiplier: float = 1.0
    roll_fraction: float | None = None
    # Item attributes and enchantments need their item-level/tier/roll
    # inputs folded into the scalar before other consumers reuse them.
    # Actor bonuses authored directly on templates already have final values.
    materialize_source: bool = True


@dataclass(frozen=True)
class PlayerCombatStats:
    """Damage range and equipment-owned skill modifier programs for one cast."""

    minimum_damage: float
    maximum_damage: float
    class_base_damage: float
    equipment_minimum_damage: float
    equipment_maximum_damage: float
    skill_modifier_sources: tuple[CombatModifier, ...]
    critical_hit_chance: float = 0.0
    critical_rating: float = 0.0
    attack_speed: float = 1.0
    critical_damage_multiplier: float = 2.0
    maximum_health: int = 1
    maximum_resource: int = 100
    health_regeneration_fraction: float = 0.0
    health_regeneration_absolute: float = 0.0
    health_regeneration_interval: float = 1.0
    resource_regeneration_fraction: float = 0.0
    resource_regeneration_absolute: float = 0.0
    resource_regeneration_interval: float = 1.0
    resource_regeneration_delay: float = 0.0
    armor_rating: float = 0.0
    resistance_ratings: tuple[tuple[str, float], ...] = ()
    incoming_damage_multipliers: tuple[tuple[str, float], ...] = ()
    life_leech_programs: tuple[tuple[float, frozenset[str]], ...] = ()
    damage_reflection_fraction: float = 0.0
    block_chance: float = 0.0
    block_damage_reduction: float = 0.0
    base_movement_speed: float = 5.0
    movement_speed: float = 5.0
    movement_bonus_speed: float = 0.0
    mount_duration_scale: float = 1.0

    def incoming_damage_multiplier(self, damage_type: str) -> float:
        """Return the level-scaled mitigation multiplier for one element."""

        wanted = str(damage_type or "Physical").casefold()
        if wanted == "andermagic":
            wanted = "darkmagic"
        return next(
            (
                max(0.0, min(1.0, float(value)))
                for name, value in self.incoming_damage_multipliers
                if name == wanted
            ),
            1.0,
        )

    def life_leech_fraction(self, skill_resource_id: str) -> float:
        """Return additive damage-to-health conversion for a cast skill."""

        wanted = str(skill_resource_id or "").casefold()
        return max(
            0.0,
            sum(
                fraction
                for fraction, skill_ids in self.life_leech_programs
                if not skill_ids or wanted in skill_ids
            ),
        )


@dataclass(frozen=True)
class StaticPlayerCombatDatabase:
    database_path: Path
    database_revision: tuple[int, int]
    base_damage_by_class_level: dict[tuple[str, int], float]
    base_health_by_class_level: dict[tuple[str, int], float]
    base_resource_by_class_level: dict[tuple[str, int], float]
    player_template_damage: dict[str, tuple[float, float]]
    player_template_recovery: dict[
        str, tuple[float, float, float]
    ]
    player_template_resource_recovery: dict[
        str, tuple[float, float, float, float]
    ]
    player_class_health_factors: dict[str, float]
    player_template_defenses: dict[
        str, tuple[float, tuple[tuple[str, float], ...]]
    ]
    actor_variables: dict[str, float]
    progression_factors: dict[int, dict[str, float]]
    status_effect_programs: dict[int, tuple[CombatModifier, ...]]
    status_effect_ids: dict[str, int]
    status_effect_max_stack_sizes: dict[int, int]
    enchantments: dict[str, _EnchantmentDefinition]
    enchantment_power_factors: dict[
        int, tuple[tuple[float, float], ...]
    ]
    base_enchantment_power_factors: dict[
        int, tuple[tuple[float, float], ...]
    ]
    items: dict[str, _ItemDefinition]
    maximum_critical_chance: float
    maximum_critical_damage: float
    base_critical_damage: float
    maximum_block_chance: float
    maximum_block_damage_reduction: float
    player_template_movement: dict[str, float]
    movement_speed_limits: tuple[float, float]
    player_skill_speed_minimum: dict[str, float]

    def progression_factor(self, level: int, factor_name: str) -> float:
        """Resolve one native ``LevelModifier`` progression factor."""

        requested_level = max(1, int(level))
        factors = self.progression_factors.get(requested_level)
        if factors is None and self.progression_factors:
            nearest = min(
                self.progression_factors,
                key=lambda candidate: (
                    abs(candidate - requested_level), candidate
                ),
            )
            factors = self.progression_factors[nearest]
        if not factors:
            return 1.0
        return max(
            0.0,
            float(factors.get(str(factor_name).casefold(), 1.0)),
        )

    def class_health_factor(self, class_name: str) -> float:
        """Return the native ``UseClassHitpoints`` item multiplier."""

        return max(
            0.0,
            float(
                self.player_class_health_factors.get(
                    str(class_name or "").casefold(), 1.0
                )
            ),
        )

    def base_damage(self, class_name: str, level: int) -> float:
        class_key = str(class_name or "").casefold()
        requested_level = max(1, int(level))
        exact = self.base_damage_by_class_level.get(
            (class_key, requested_level)
        )
        if exact is not None:
            return max(1.0, exact)
        available = sorted(
            candidate_level
            for candidate_class, candidate_level
            in self.base_damage_by_class_level
            if candidate_class == class_key
        )
        if available:
            nearest = min(
                available,
                key=lambda candidate: (
                    abs(candidate - requested_level),
                    candidate,
                ),
            )
            return max(
                1.0,
                self.base_damage_by_class_level[(class_key, nearest)],
            )
        template = self.player_template_damage.get(class_key, (1.0, 1.0))
        return max(1.0, sum(template) / 2.0)

    def _base_progression_value(
        self,
        values: dict[tuple[str, int], float],
        class_name: str,
        level: int,
        fallback: float,
    ) -> float:
        class_key = str(class_name or "").casefold()
        requested_level = max(1, int(level))
        exact = values.get((class_key, requested_level))
        if exact is not None:
            return max(1.0, exact)
        available = sorted(
            candidate_level
            for candidate_class, candidate_level in values
            if candidate_class == class_key
        )
        if available:
            nearest = min(
                available,
                key=lambda candidate: (
                    abs(candidate - requested_level), candidate
                ),
            )
            return max(1.0, values[(class_key, nearest)])
        return max(1.0, float(fallback))

    def base_health(self, class_name: str, level: int) -> float:
        return self._base_progression_value(
            self.base_health_by_class_level,
            class_name,
            level,
            100.0,
        )

    def base_resource(self, class_name: str, level: int) -> float:
        return self._base_progression_value(
            self.base_resource_by_class_level,
            class_name,
            level,
            100.0,
        )

    def player_recovery(
        self, class_name: str
    ) -> tuple[float, float, float]:
        return self.player_template_recovery.get(
            str(class_name or "").casefold(),
            (0.0, 0.0, 1.0),
        )

    def player_resource_recovery(
        self, class_name: str
    ) -> tuple[float, float, float, float]:
        """Return relative/absolute amount, cadence, and post-cast delay."""

        return self.player_template_resource_recovery.get(
            str(class_name or "").casefold(),
            (0.0, 0.0, 1.0, 0.0),
        )

    def enchantment_power_factor(
        self,
        tier: int,
        rarity: int,
        roll: float,
    ) -> float:
        factors = self.enchantment_power_factors.get(
            int(tier),
            self.enchantment_power_factors.get(0),
        )
        if not factors:
            return 1.0
        rarity = max(0, min(len(factors) - 1, int(rarity)))
        minimum, maximum = factors[rarity]
        fraction = max(0.0, min(1.0, float(roll)))
        return minimum + fraction * (maximum - minimum)

    def base_enchantment_power_factor(
        self,
        tier: int,
        rarity: int,
        roll: float,
    ) -> float:
        """Constrain a native ``BaseEnchantments`` quality roll.

        The client keeps base-stat quality boundaries in
        ``_Template_EnchantmentBaseTiers``. These deliberately differ from
        the boundaries used by concrete/random enchantments.
        """

        factors = self.base_enchantment_power_factors.get(
            int(tier), self.base_enchantment_power_factors.get(0)
        )
        if not factors:
            return 1.0
        rarity = max(0, min(len(factors) - 1, int(rarity)))
        minimum, maximum = factors[rarity]
        fraction = max(0.0, min(1.0, float(roll)))
        return minimum + fraction * (maximum - minimum)


def _parse_program(value: object) -> tuple[CombatModifier, ...]:
    result = []
    for raw in str(value or "").split(";"):
        kind, separator, argument_text = raw.partition(":")
        if not separator or not kind.strip():
            continue
        result.append(
            CombatModifier(
                kind=kind.strip(),
                arguments=tuple(
                    argument.strip()
                    for argument in argument_text.split(",")
                ),
            )
        )
    return tuple(result)


def _split_ids(value: object) -> tuple[str, ...]:
    return tuple(
        part.strip() for part in str(value or "").split(";") if part.strip()
    )


def _plain_enchantment_ids(value: object) -> tuple[str, ...]:
    return tuple(
        part.strip()
        for part in str(value or "").split(";")
        if part.strip() and "|" not in part
    )


def _open_read_only(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    return database


@lru_cache(maxsize=4)
def _load_static_database(
    database_path: str,
    modified_ns: int,
    file_size: int,
) -> StaticPlayerCombatDatabase:
    path = Path(database_path)
    with _open_read_only(path) as database:
        progression_rows = tuple(database.execute(
            "SELECT CharClass, Level, BaseDamage, BaseHP, BaseMana "
            "FROM _Template_XPLevels"
        ))
        base_damage = {
            (str(row["CharClass"]).casefold(), int(row["Level"])): float(
                row["BaseDamage"] or 0.0
            )
            for row in progression_rows
        }
        base_health = {
            (str(row["CharClass"]).casefold(), int(row["Level"])): float(
                row["BaseHP"] or 0.0
            )
            for row in progression_rows
        }
        base_resource = {
            (str(row["CharClass"]).casefold(), int(row["Level"])): float(
                row["BaseMana"] or 0.0
            )
            for row in progression_rows
        }
        player_rows = tuple(database.execute(
            "SELECT CharClass, MinDamage, MaxDamage, "
            "HitPointsRegeneration, AbsoluteHitPointsRegeneration, "
            "HitPointsRegFrequency, SkillResourceRegeneration, "
            "AbsoluteSkillResourceRegeneration, SkillResourceRegFrequency, "
            "SkillResourceRegDelay, ClassHitpointsFactor, Armor, Resistances, RunSpeed, SkillSpeedMin "
            "FROM _Template_Player"
        ))
        player_templates = {
            str(row["CharClass"]).casefold(): (
                float(row["MinDamage"] or 0.0),
                float(row["MaxDamage"] or 0.0),
            )
            for row in player_rows
        }
        player_recovery = {
            str(row["CharClass"]).casefold(): (
                max(0.0, float(row["HitPointsRegeneration"] or 0.0)),
                max(
                    0.0,
                    float(row["AbsoluteHitPointsRegeneration"] or 0.0),
                ),
                max(0.05, float(row["HitPointsRegFrequency"] or 1.0)),
            )
            for row in player_rows
        }
        player_resource_recovery = {
            str(row["CharClass"]).casefold(): (
                float(row["SkillResourceRegeneration"] or 0.0),
                float(row["AbsoluteSkillResourceRegeneration"] or 0.0),
                max(
                    0.05,
                    float(row["SkillResourceRegFrequency"] or 1.0),
                ),
                max(0.0, float(row["SkillResourceRegDelay"] or 0.0)),
            )
            for row in player_rows
        }
        player_class_health_factors = {
            str(row["CharClass"]).casefold(): max(
                0.0, float(row["ClassHitpointsFactor"] or 1.0)
            )
            for row in player_rows
        }
        player_defenses = {}
        for row in player_rows:
            resistances = []
            for encoded in str(row["Resistances"] or "").split(";"):
                name, separator, raw_value = encoded.partition(":")
                if not separator or not name.strip():
                    continue
                try:
                    value = float(raw_value)
                except ValueError:
                    value = 0.0
                normalized = name.strip().casefold()
                if normalized == "andermagic":
                    normalized = "darkmagic"
                resistances.append((normalized, max(0.0, value)))
            player_defenses[str(row["CharClass"]).casefold()] = (
                max(0.0, float(row["Armor"] or 0.0)),
                tuple(resistances),
            )
        actor_variables = {
            str(row["Id"]).casefold(): float(row["InitialValue"] or 0.0)
            for row in database.execute(
                "SELECT Id, InitialValue FROM _Template_ActorVariables"
            )
        }
        progression_factor_names = (
            "DamageFactorAbsolute",
            "DamageFactorRelative",
            "AttacksPerSecondFactorAbsolute",
            "AttacksPerSecondFactorRelative",
            "MovementSpeedFactorAbsolute",
            "MovementSpeedFactorRelative",
            "CriticalFactorAbsolute",
            "CriticalFactorRelative",
            "CriticalChanceFactor",
            "CriticalDamageFactor",
            "ArmorFactorAbsolute",
            "ArmorFactorRelative",
            "ResistanceFactorAbsolute",
            "ResistanceFactorRelative",
            "HealthPointsFactorAbsolute",
            "HealthPointsFactorRelative",
            "BlockFactorAbsolute",
            "BlockFactorRelative",
            "BlockChanceFactor",
            "BlockStrengthFactor",
            "MaxResourceFactorAbsolute",
            "MaxResourceFactorRelative",
            "ArmorDamageReductionValue",
            "ResistanceDamageReductionValue",
        )
        progression_factors = {
            int(row["Level"]): {
                name.casefold(): float(row[name] or 0.0)
                for name in progression_factor_names
            }
            for row in database.execute(
                "SELECT Level, "
                + ", ".join(progression_factor_names)
                + " FROM _Template_Progressions"
            )
        }
        status_rows = tuple(database.execute(
            "SELECT rowid, Id, MaxStackSize, StartModifiers "
            "FROM _Template_StatusEffect"
        ))
        status_programs = {
            int(row["rowid"]) - 1: _parse_program(row["StartModifiers"])
            for row in status_rows
        }
        status_effect_ids = {
            str(row["Id"]): int(row["rowid"]) - 1
            for row in status_rows
        }
        status_effect_max_stack_sizes = {
            int(row["rowid"]) - 1: max(
                1, int(row["MaxStackSize"] or 1)
            )
            for row in status_rows
        }
        enchantments = {
            str(row["Id"]): _EnchantmentDefinition(
                modifiers=_parse_program(row["Modifiers"]),
                scale_absolute=(
                    str(row["ScalingMode"] or "").casefold()
                    in {"absolute", "relative"}
                ),
                use_tier_factor=bool(row["UseTierFactor"]),
                minimum_item_level=max(1, int(row["MinItemLevel"] or 1)),
                maximum_item_level=max(0, int(row["MaxItemLevel"] or 0)),
                scale_reference_level=max(
                    1, int(row["ScaleReferenceLevel"] or 1)
                ),
            )
            for row in database.execute(
                """
                SELECT Id, Modifiers, ScalingMode, UseTierFactor,
                       MinItemLevel, MaxItemLevel, ScaleReferenceLevel
                FROM _Template_Enchantment
                """
            )
        }
        rarity_names = (
            "Common",
            "Uncommon",
            "Magic",
            "Rare",
            "Epic",
            "Unique",
            "Set",
            "Mythic",
        )
        enchantment_power_factors = {}
        for row in database.execute(
            "SELECT * FROM _Template_EnchantmentTiers"
        ):
            enchantment_power_factors[int(row["Tier"])] = tuple(
                (
                    float(row[f"MinPowerFactor{rarity}"] or 0.0),
                    float(row[f"MaxPowerFactor{rarity}"] or 1.0),
                )
                for rarity in rarity_names
            )
        base_enchantment_power_factors = {}
        for row in database.execute(
            "SELECT * FROM _Template_EnchantmentBaseTiers"
        ):
            base_enchantment_power_factors[int(row["Tier"])] = tuple(
                (
                    float(row[f"MinPowerFactor{rarity}"] or 0.0),
                    float(row[f"MaxPowerFactor{rarity}"] or 1.0),
                )
                for rarity in rarity_names
            )
        items = {}
        for row in database.execute(
            """
            SELECT Id, ItemCategory, MinDamage, MaxDamage,
                   ItemLevelScalingType, BaseEnchantments, Enchantments,
                   UniqueModifier, BaseModifier
            FROM _Template_Item
            """
        ):
            template_id = str(row["Id"])
            items[template_id] = _ItemDefinition(
                template_id=template_id,
                category=str(row["ItemCategory"] or ""),
                minimum_damage=float(row["MinDamage"] or 0.0),
                maximum_damage=float(row["MaxDamage"] or 0.0),
                scale_with_level=(
                    str(row["ItemLevelScalingType"] or "").casefold()
                    != "noscaling"
                ),
                base_enchantment_ids=_split_ids(
                    row["BaseEnchantments"]
                ),
                template_enchantment_ids=_plain_enchantment_ids(
                    row["Enchantments"]
                ),
                modifiers=(
                    _parse_program(row["UniqueModifier"])
                    + _parse_program(row["BaseModifier"])
                ),
            )
        movement_limits = database.execute(
            "SELECT LowerLimit, UpperLimit FROM _Template_ActorAttributes "
            "WHERE Id='MovementSpeed'"
        ).fetchone()
        global_values = database.execute(
            "SELECT MaxCriticalChance, MaxCriticalDamage, "
            "CriticalDamageBonus, MaxBlockChance, "
            "MaxBlockDamageReduction FROM _Globals LIMIT 1"
        ).fetchone()
    return StaticPlayerCombatDatabase(
        database_path=path,
        database_revision=(int(modified_ns), int(file_size)),
        base_damage_by_class_level=base_damage,
        base_health_by_class_level=base_health,
        base_resource_by_class_level=base_resource,
        player_template_damage=player_templates,
        player_template_movement={
            str(row["CharClass"]).casefold(): float(row["RunSpeed"])
            for row in player_rows
        },
        player_skill_speed_minimum={
            str(row["CharClass"]).casefold(): max(0.1, float(row["SkillSpeedMin"]))
            for row in player_rows
        },
        movement_speed_limits=tuple(float(value) for value in movement_limits),
        player_template_recovery=player_recovery,
        player_template_resource_recovery=player_resource_recovery,
        player_class_health_factors=player_class_health_factors,
        player_template_defenses=player_defenses,
        actor_variables=actor_variables,
        progression_factors=progression_factors,
        status_effect_programs=status_programs,
        status_effect_ids=status_effect_ids,
        status_effect_max_stack_sizes=status_effect_max_stack_sizes,
        enchantments=enchantments,
        enchantment_power_factors=enchantment_power_factors,
        base_enchantment_power_factors=base_enchantment_power_factors,
        items=items,
        maximum_critical_chance=max(
            0.0, float(global_values["MaxCriticalChance"] or 0.8)
        ),
        maximum_critical_damage=max(
            1.0, float(global_values["MaxCriticalDamage"] or 6.0)
        ),
        base_critical_damage=max(
            1.0, float(global_values["CriticalDamageBonus"] or 2.0)
        ),
        maximum_block_chance=max(
            0.0, float(global_values["MaxBlockChance"] or 0.8)
        ),
        maximum_block_damage_reduction=max(
            0.0,
            float(global_values["MaxBlockDamageReduction"] or 1.0),
        ),
    )


_COMBAT_DATABASE_RECHECK_SECONDS = 1.0
# Last observed ``(st_mtime_ns, st_size)`` per resolved database path.
_combat_database_revisions: dict[str, tuple[int, int]] = {}
_combat_database_checked_at: dict[str, float] = {}
_combat_database_rechecks: set[str] = set()
_combat_database_revision_lock = threading.Lock()


def prime_player_combat_database(
    database_path: str | Path,
) -> StaticPlayerCombatDatabase:
    """Load immutable combat rows once for the current database revision.

    Only the first call for a path touches the filesystem on the caller's
    thread. Later calls return the last observed revision at once, and at
    most once per second a background thread re-checks the file and parses a
    replaced database before callers switch to it. Casts and monster ticks
    used to ``stat()`` the file themselves: on 2026-10-24 one such call took
    three seconds on a volume spanning two hard disks, and the cast and the
    map's simulation, both waiting on it, froze combat for that long.
    """

    path = _resolved_combat_database_path(str(database_path))
    key = str(path)
    now = time.perf_counter()
    with _combat_database_revision_lock:
        revision = _combat_database_revisions.get(key)
        recheck = (
            revision is not None
            and key not in _combat_database_rechecks
            and now - _combat_database_checked_at.get(key, now)
            >= _COMBAT_DATABASE_RECHECK_SECONDS
        )
        if recheck:
            _combat_database_rechecks.add(key)
    if revision is None:
        revision = _combat_database_revision(path)
        with _combat_database_revision_lock:
            _combat_database_revisions[key] = revision
            _combat_database_checked_at[key] = now
    elif recheck:
        threading.Thread(
            target=_recheck_combat_database,
            args=(path, key),
            name="combat-database-recheck",
            daemon=True,
        ).start()
    return _load_static_database(key, *revision)


def _combat_database_revision(path: Path) -> tuple[int, int]:
    stat = resident_content.stat(path)
    return int(stat.st_mtime_ns), int(stat.st_size)


def _recheck_combat_database(path: Path, key: str) -> None:
    """Observe the database revision away from the combat hot path."""

    revision = None
    try:
        revision = _combat_database_revision(path)
        _load_static_database(key, *revision)
    except Exception as exc:
        # Keep serving the revision already loaded; the next check retries.
        revision = None
        print(
            f"[PlayerCombat] could not re-check {key}: "
            f"{type(exc).__name__}: {exc}"
        )
    finally:
        with _combat_database_revision_lock:
            if revision is not None:
                _combat_database_revisions[key] = revision
            _combat_database_checked_at[key] = time.perf_counter()
            _combat_database_rechecks.discard(key)


@lru_cache(maxsize=8)
def _resolved_combat_database_path(database_path: str) -> Path:
    """Resolve a stable DB pathname outside the combat hot path.

    ``Path.resolve()`` performs filesystem work on Windows. Calling it for
    every projectile impact serialized otherwise-independent combat workers
    inside the OS path resolver. The database revision is still checked by
    ``stat()`` above, so replacing the DB continues to invalidate the parsed
    row cache without putting path canonicalization in every hit.
    """

    return resident_content.resolved(database_path)


def _clean_token(value: object) -> str:
    return (
        str(value or "")
        .split("#", 1)[0]
        .split("<", 1)[0]
        .strip()
    )


@lru_cache(maxsize=16384)
def _parsed_numeric_token(token: str):
    """Parse one modifier argument once: None, ("detail", i), ("variable",
    name) or ("range", low, high). A stat calculation resolves the same
    authored text hundreds of times, so tokenizing it on every resolution was
    most of a recalculation's cost."""

    value = _clean_token(token)
    if not value:
        return None
    if value.startswith("$"):
        reference = value[1:]
        if reference.isdigit():
            return ("detail", int(reference))
        return ("variable", reference.casefold())
    numbers = []
    for part in value.split("|"):
        try:
            numbers.append(float(part))
        except ValueError:
            continue
    if not numbers:
        return None
    return ("range", min(numbers), max(numbers))


def _numeric_range(
    token: object,
    *,
    details: tuple[float, ...],
    variables: dict[str, float],
) -> tuple[float, float] | None:
    parsed = _parsed_numeric_token(str(token or ""))
    if parsed is None:
        return None
    kind = parsed[0]
    if kind == "range":
        return (parsed[1], parsed[2])
    if kind == "detail":
        index = parsed[1]
        number = float(details[index]) if index < len(details) else 0.0
    else:
        # Named collection variables and numeric effect parameters are
        # separate operands. Zero must not fall back to the pet's $0.
        number = float(variables.get(parsed[1], 0.0))
    return (number, number)


def _resolved_range(
    instruction: _Instruction,
    variables: dict[str, float],
) -> tuple[float, float] | None:
    if not instruction.modifier.arguments:
        return None
    result = _numeric_range(
        instruction.modifier.arguments[0],
        details=instruction.details,
        variables=variables,
    )
    if result is None:
        return None
    if instruction.roll_fraction is not None:
        fraction = max(
            0.0, min(1.0, float(instruction.roll_fraction))
        )
        selected = result[0] + fraction * (result[1] - result[0])
        result = (selected, selected)
    result = (
        result[0] * float(instruction.amount_multiplier),
        result[1] * float(instruction.amount_multiplier),
    )
    operation = (
        instruction.modifier.arguments[1].casefold()
        if len(instruction.modifier.arguments) > 1
        else "absolute"
    )
    scale = max(1, int(instruction.stack_count))
    if operation == "absolute" or instruction.scale_relative:
        scale *= max(0.0, float(instruction.absolute_scale))
    return (result[0] * scale, result[1] * scale)


def _status_group_duration_scale(
    instructions: tuple[_Instruction, ...],
    variables: dict[str, float],
    group: str,
) -> float:
    """``1 + relative`` of every StatusEffectGroupDuration on a status group.

    The amounts add up: the client's StatusEffectGroupDuration::Apply sums them
    per group (0x1409f0b77), so a -50% cloak and a -31% talent make -81%.
    """

    total = 0.0
    for instruction in instructions:
        modifier = instruction.modifier
        arguments = modifier.arguments
        if (
            modifier.kind.casefold() != "statuseffectgroupduration"
            or len(arguments) < 3
            or arguments[1].casefold() != "relative"
            or _clean_token(arguments[2]).casefold() != group
        ):
            continue
        amount = _resolved_range(instruction, variables)
        if amount is not None:
            total += sum(amount) / 2.0
    return max(0.0, 1.0 + total)


def _scopes(arguments: tuple[str, ...]) -> frozenset[str]:
    try:
        return _cached_scopes(tuple(arguments))
    except TypeError:  # an unhashable argument: compute directly
        return _uncached_scopes(arguments)


@lru_cache(maxsize=16384)
def _cached_scopes(arguments: tuple[str, ...]) -> frozenset[str]:
    # Every stat aggregator asks for the same authored modifier's scopes;
    # cleaning the text each time was a quarter of a recalculation.
    return _uncached_scopes(arguments)


def _uncached_scopes(arguments) -> frozenset[str]:
    return frozenset(
        _clean_token(value).casefold()
        for value in arguments[2:]
        if _clean_token(value)
    )


def _damage_scope(scopes: frozenset[str]) -> tuple[bool, bool]:
    explicit = scopes.intersection(("min", "max"))
    if not explicit:
        return (True, True)
    return ("min" in explicit, "max" in explicit)


def _apply_variable_programs(
    instructions: tuple[_Instruction, ...],
    initial_values: dict[str, float],
) -> dict[str, float]:
    variables = dict(initial_values)
    for instruction in instructions:
        modifier = instruction.modifier
        if (
            modifier.kind.casefold() != "actorvariable"
            or len(modifier.arguments) < 3
        ):
            continue
        variable_id = _clean_token(modifier.arguments[2]).casefold()
        amount = _resolved_range(instruction, variables)
        if not variable_id or amount is None:
            continue
        value = sum(amount) / 2.0
        current = float(variables.get(variable_id, 0.0))
        operation = modifier.arguments[1].casefold()
        variables[variable_id] = (
            current * (1.0 + value)
            if operation == "relative"
            else current + value
        )
    return variables


def _absolute_progression_scale(
    database: StaticPlayerCombatDatabase,
    modifier: CombatModifier,
    level: int,
    *,
    default_scaled: bool,
    reference_level: int = 1,
) -> float:
    """Resolve the factor named by the modifier's ``LevelModifier``.

    Scaled enchantments use the operation-specific native progression table.
    For example, an ``ItemHealthPoints:...,relative`` enchantment uses
    ``HealthPointsFactorRelative`` even though the enchantment row's
    ``ScalingMode`` is ``Absolute``.
    """

    arguments = modifier.arguments
    operation = (
        arguments[1].casefold() if len(arguments) > 1 else "absolute"
    )
    suffix = "Relative" if operation == "relative" else "Absolute"
    tokens = tuple(_clean_token(value).casefold() for value in arguments[2:])
    explicit_level_modifier = "levelmodifier" in tokens
    factor_name = next(
        (
            _clean_token(value)
            for value in arguments[2:]
            if _clean_token(value).casefold().endswith(
                ("factorabsolute", "factorrelative")
            )
        ),
        "",
    )
    if not factor_name and (default_scaled or explicit_level_modifier):
        kind = modifier.kind.casefold()
        scopes = _scopes(arguments)
        if kind in {"damage", "itemdamage", "weapondamagebonus"}:
            factor_name = f"DamageFactor{suffix}"
        elif kind in {"speed", "itemspeed"}:
            factor_name = (
                f"AttacksPerSecondFactor{suffix}"
                if scopes.intersection({"skills", "allplayerskills"})
                else f"MovementSpeedFactor{suffix}"
            )
        elif kind in {"criticalvalue", "itemcriticalvalue"}:
            factor_name = f"CriticalFactor{suffix}"
        elif kind in {"armor", "itemarmor"}:
            factor_name = f"ArmorFactor{suffix}"
        elif kind in {"resistance", "itemresistance"}:
            factor_name = f"ResistanceFactor{suffix}"
        elif kind in {"healthpoints", "itemhealthpoints", "maxhealthpoints"}:
            factor_name = f"HealthPointsFactor{suffix}"
        elif kind in {
            "block", "blockvalue", "itemblockvalue",
            "smallweaponblock", "largeweaponblock", "offhandblock",
        }:
            factor_name = f"BlockFactor{suffix}"
        elif kind == "maxskillresource":
            factor_name = f"MaxResourceFactor{suffix}"
    if not factor_name:
        return 1.0
    target = database.progression_factor(level, factor_name)
    reference = database.progression_factor(reference_level, factor_name)
    if reference == 0.0:
        return target
    return target / reference


def _status_instructions(
    database: StaticPlayerCombatDatabase,
    sources: tuple[object, ...],
    *,
    level: int,
) -> tuple[_Instruction, ...]:
    result = []
    seen_integer_ids: set[int] = set()
    explicit_effect_ids = {
        int(source.effect_id)
        for source in sources
        if hasattr(source, "effect_id")
    }
    for source in sources:
        if hasattr(source, "kind") and hasattr(source, "arguments"):
            modifier = CombatModifier(
                str(source.kind),
                tuple(str(value) for value in source.arguments),
                str(getattr(source, "source_set_id", "")),
            )
            result.append(
                _Instruction(
                    modifier,
                    absolute_scale=_absolute_progression_scale(
                        database,
                        modifier,
                        level,
                        default_scaled=False,
                    ),
                )
            )
            continue
        if hasattr(source, "effect_id"):
            effect_id = int(source.effect_id)
            # ``field_18`` is also the wire occurrence/generation used when
            # an active effect is replaced. It is not an unbounded gameplay
            # multiplier: max-one effects may advance it on every refresh.
            # Clamp it to the authored status stack limit before applying the
            # modifier program, while preserving real stack effects such as
            # the Wisdom damage ranks.
            stack_count = min(
                database.status_effect_max_stack_sizes.get(effect_id, 1),
                max(1, int(getattr(source, "field_18", 0)) + 1),
            )
            details = tuple(
                float(value) for value in getattr(source, "values", ())
            )
        else:
            try:
                effect_id = int(source)
            except (TypeError, ValueError):
                continue
            if effect_id in seen_integer_ids:
                continue
            seen_integer_ids.add(effect_id)
            stack_count = 1
            details = ()
        result.extend(
            _Instruction(
                modifier,
                stack_count=stack_count,
                details=details,
                absolute_scale=_absolute_progression_scale(
                    database,
                    modifier,
                    level,
                    default_scaled=False,
                ),
            )
            for modifier in database.status_effect_programs.get(
                effect_id, ()
            )
        )
    # Jewel and rune programs commonly attach a child ActorStatusEffect whose
    # modifiers contain the actual stats or skill alterations. Expand that
    # authored graph so server damage/stat authority matches the client.
    cursor = 0
    seen_nested: set[tuple[int, int, tuple[float, ...]]] = set()
    while cursor < len(result) and cursor < 256:
        instruction = result[cursor]
        cursor += 1
        modifier = instruction.modifier
        if (
            modifier.kind.casefold() != "actorstatuseffect"
            or len(modifier.arguments) < 2
        ):
            continue
        # Event registrations are not active children. For example, the
        # mage fame talent's ON:LowHealth cyanite skin must contribute only
        # when an explicit active runtime entry is among the sources.
        if _clean_token(modifier.arguments[0]).casefold() not in {"addae", "addle"}:
            continue
        metadata = {
            name.strip().casefold(): _clean_token(value)
            for token in modifier.arguments[2:]
            for name, separator, value in (token.partition(":"),)
            if separator
        }
        if metadata.get("on", "").casefold() not in {"", "immediate", "immediatemanaged"}:
            continue
        try:
            guaranteed = float(metadata.get("c", "1")) >= 1.0
        except ValueError:
            guaranteed = False
        if not guaranteed:
            continue
        effect_id = database.status_effect_ids.get(
            _clean_token(modifier.arguments[1])
        )
        if effect_id is None or effect_id in explicit_effect_ids:
            continue
        indexed: dict[int, float] = {}
        for token in modifier.arguments[2:]:
            name, separator, raw_value = token.partition(":")
            if not separator or not name.startswith("$"):
                continue
            try:
                index = int(name[1:])
            except ValueError:
                continue
            value_token = _clean_token(raw_value)
            try:
                if value_token.startswith("$"):
                    parent_index = int(value_token[1:])
                    value = float(instruction.details[parent_index])
                else:
                    value = float(value_token)
            except (ValueError, IndexError):
                value = 0.0
            indexed[index] = value
        detail_count = max(5, max(indexed, default=-1) + 1)
        details = tuple(
            indexed.get(index, 0.0) for index in range(detail_count)
        )
        key = (effect_id, instruction.stack_count, details)
        if key in seen_nested:
            continue
        seen_nested.add(key)
        result.extend(
            _Instruction(
                child,
                stack_count=instruction.stack_count,
                details=details,
                absolute_scale=_absolute_progression_scale(
                    database,
                    child,
                    level,
                    default_scaled=False,
                ),
            )
            for child in database.status_effect_programs.get(effect_id, ())
        )
    return tuple(result)


def _scaled_item_instruction(
    database: StaticPlayerCombatDatabase,
    modifier: CombatModifier,
    *,
    item_level: int,
    scale_absolute: bool,
    reference_level: int = 1,
    amount_multiplier: float = 1.0,
    roll_fraction: float | None = None,
    materialize_source: bool = True,
) -> _Instruction:
    return _Instruction(
        modifier=modifier,
        absolute_scale=_absolute_progression_scale(
            database,
            modifier,
            item_level,
            default_scaled=scale_absolute,
            reference_level=reference_level,
        ),
        scale_relative=(
            scale_absolute
            and len(modifier.arguments) > 1
            and modifier.arguments[1].casefold() == "relative"
        ),
        amount_multiplier=amount_multiplier,
        roll_fraction=roll_fraction,
        materialize_source=materialize_source,
    )


def _materialized_skill_source(
    instruction: _Instruction,
    variables: dict[str, float],
) -> CombatModifier:
    """Return the concrete item-instance program used by skill timing.

    Damage calculation consumes ``_Instruction`` directly, but the skill
    evaluator receives portable ``CombatModifier`` values.  Exporting the
    database template string here used to discard item level, enchantment
    tier, and the concrete roll and made every instance behave like the same
    maximum template roll.
    """

    modifier = instruction.modifier
    if not instruction.materialize_source or not modifier.arguments:
        return modifier
    amount = _resolved_range(instruction, variables)
    if amount is None:
        return modifier
    # Unrolled base-enchantment ranges use the same deterministic upper value
    # as the existing local item bootstrap. Concrete instance rolls resolve
    # to an exact one-element range before reaching this point.
    literal = format(float(amount[1]), ".17g")
    materialized = CombatModifier(
        kind=modifier.kind,
        arguments=(literal, *modifier.arguments[1:]),
    )
    # Every stats miss materializes the ~380 equipped-item programs again.
    # Handing back the equal frozen object from the previous calculation keeps
    # the identity memos downstream (skill geometry, conditional sources,
    # signatures) hitting after each buff change.
    interned = _MATERIALIZED_SOURCES.get(materialized)
    if interned is None:
        if len(_MATERIALIZED_SOURCES) >= 65536:
            _MATERIALIZED_SOURCES.clear()
        interned = _MATERIALIZED_SOURCES[materialized] = materialized
    return interned


_MATERIALIZED_SOURCES: dict[CombatModifier, CombatModifier] = {}


_SCALED_ITEM_ATTRIBUTES = frozenset({
    "itemdamage", "itemcriticalvalue", "itemarmor", "itemresistance",
    "itemhealthpoints", "itemblockvalue", "itemspeed",
})


def _item_instructions(
    database: StaticPlayerCombatDatabase,
    item,
) -> tuple[_Instruction, ...]:
    definition = database.items.get(str(item.template_id))
    if definition is None:
        return ()
    item_level = max(1, int(getattr(item, "item_level", 1)))
    result = [
        _scaled_item_instruction(
            database,
            modifier,
            item_level=item_level,
            # Item attributes use the item's progression even when authored
            # directly in UniqueModifier. Actor bonuses (e.g. Damage:5000)
            # already carry their final values and must stay unscaled.
            scale_absolute=(
                definition.scale_with_level
                and modifier.kind.casefold() in _SCALED_ITEM_ATTRIBUTES
            ),
            materialize_source=(modifier.kind.casefold() in _SCALED_ITEM_ATTRIBUTES),
        )
        for modifier in definition.modifiers
    ]
    # field_18 is ItemInfo.seedIdx. For BaseEnchantments the native GameItem
    # resets a dSFMT stream from ``int(float32(seedIdx) * 0.5f)``, then
    # consumes one quality roll per customizable range in authored order.
    # The client sees that concrete value, not the range midpoint.
    base_rolls = deterministic_base_enchantment_rolls(
        int(getattr(item, "field_18", 0))
    )
    for enchantment_id in definition.base_enchantment_ids:
        enchantment = database.enchantments.get(enchantment_id)
        if enchantment is None:
            continue
        for modifier in enchantment.modifiers:
            numeric_range = _numeric_range(
                modifier.arguments[0] if modifier.arguments else "",
                details=(),
                variables={},
            )
            roll_fraction = None
            if (
                numeric_range is not None
                and numeric_range[0] != numeric_range[1]
            ):
                roll_fraction = next(base_rolls)
                # ``GameItem::ComputeModifierQualityRange`` constrains the
                # dSFMT quality by the item's enchantment tier and rarity
                # before ``SetupModifiers`` interpolates the authored value
                # range. Treating this as a raw 0..1 roll makes equipped
                # base stats disagree with the values materialized locally
                # by the client from the same ItemInfo packet.
                if enchantment.use_tier_factor:
                    roll_fraction = database.base_enchantment_power_factor(
                        int(getattr(item, "field_38", 0)),
                        int(getattr(item, "field_08", 0)),
                        roll_fraction,
                    )
            result.append(_scaled_item_instruction(
                database,
                modifier,
                item_level=enchantment.effective_level(item_level),
                scale_absolute=enchantment.scale_absolute,
                reference_level=enchantment.scale_reference_level,
                roll_fraction=roll_fraction,
            ))
    concrete_enchantments = tuple(
        getattr(item, "enchantments", ())
    )
    # Item.Enchantments describes a fixed/random instance composition. The
    # concrete packet list supplies its rolled values. Older bootstrap items
    # predate that serializer, so retain the template list only as a fallback
    # when no concrete lines exist.
    if not concrete_enchantments:
        for enchantment_id in definition.template_enchantment_ids:
            enchantment = database.enchantments.get(enchantment_id)
            if enchantment is None:
                continue
            result.extend(
                _scaled_item_instruction(
                    database,
                    modifier,
                    item_level=enchantment.effective_level(item_level),
                    scale_absolute=enchantment.scale_absolute,
                    reference_level=enchantment.scale_reference_level,
                )
                for modifier in enchantment.modifiers
            )
    item_rarity = max(0, int(getattr(item, "field_08", 0)))
    for concrete in concrete_enchantments:
        enchantment = database.enchantments.get(
            str(getattr(concrete, "template_id", ""))
        )
        if enchantment is None:
            continue
        rolled_value = float(getattr(concrete, "value", 0.0))
        origin_rarity = max(
            0, int(getattr(concrete, "kind", item_rarity))
        )
        if enchantment.use_tier_factor:
            amount_multiplier = database.enchantment_power_factor(
                int(getattr(concrete, "field_08", 0)),
                origin_rarity,
                rolled_value,
            )
            roll_fraction = None
        else:
            amount_multiplier = 1.0
            roll_fraction = rolled_value
        result.extend(
            _scaled_item_instruction(
                database,
                modifier,
                item_level=enchantment.effective_level(item_level),
                scale_absolute=enchantment.scale_absolute,
                reference_level=enchantment.scale_reference_level,
                amount_multiplier=amount_multiplier,
                roll_fraction=roll_fraction,
            )
            for modifier in enchantment.modifiers
        )
    for socket_template_id in getattr(item, "socketed_templates", ()):
        socket_id = str(socket_template_id or "")
        socket = database.items.get(socket_id)
        if socket is None:
            continue
        result.extend(
            _scaled_item_instruction(
                database,
                modifier,
                item_level=item_level,
                scale_absolute=socket.scale_with_level,
                materialize_source=False,
            )
            for modifier in socket.modifiers
        )
        for enchantment_id in (
            socket.base_enchantment_ids
            + socket.template_enchantment_ids
        ):
            enchantment = database.enchantments.get(enchantment_id)
            if enchantment is None:
                continue
            result.extend(
                _scaled_item_instruction(
                    database,
                    modifier,
                    item_level=enchantment.effective_level(item_level),
                    scale_absolute=enchantment.scale_absolute,
                    reference_level=enchantment.scale_reference_level,
                )
                for modifier in enchantment.modifiers
            )
    return tuple(result)


# id(database) -> (database, {item signature without id/slots: instructions}).
# Keyed by the database object, not its path: a replaced or derived database
# with the same path and revision must not see another's instructions. The
# stored reference keeps the id from being reused while the entry exists.
_ITEM_INSTRUCTION_CACHE: OrderedDict = OrderedDict()
_ITEM_INSTRUCTION_CACHE_LOCK = threading.Lock()
_ITEM_INSTRUCTION_CACHE_MAXIMUM = 4096
_ITEM_INSTRUCTION_DATABASES_MAXIMUM = 4


def _cached_item_instructions(
    database: StaticPlayerCombatDatabase,
    item,
) -> tuple[_Instruction, ...]:
    """``_item_instructions`` for an item, computed once per item state.

    The equipment half of a stat calculation: scaled attributes, rolled base
    enchantments, enchantments and sockets depend only on the item and the
    static database, while buffs change several times a second in a fight.
    A buff change used to re-parse every equipped item. The key is the
    combat signature minus the item id and slots, so identical items share.
    """

    database_key = id(database)
    item_key = _combat_item_signature(item, ())[2:]

    def entries_locked():
        stored = _ITEM_INSTRUCTION_CACHE.get(database_key)
        if stored is None or stored[0] is not database:
            stored = _ITEM_INSTRUCTION_CACHE[database_key] = (database, OrderedDict())
            while len(_ITEM_INSTRUCTION_CACHE) > _ITEM_INSTRUCTION_DATABASES_MAXIMUM:
                _ITEM_INSTRUCTION_CACHE.popitem(last=False)
        _ITEM_INSTRUCTION_CACHE.move_to_end(database_key)
        return stored[1]

    with _ITEM_INSTRUCTION_CACHE_LOCK:
        entries = entries_locked()
        cached = entries.get(item_key)
        if cached is not None:
            entries.move_to_end(item_key)
            return cached
    instructions = _item_instructions(database, item)
    with _ITEM_INSTRUCTION_CACHE_LOCK:
        entries = entries_locked()
        entries[item_key] = instructions
        while len(entries) > _ITEM_INSTRUCTION_CACHE_MAXIMUM:
            entries.popitem(last=False)
    return instructions


def _relative_total(
    current: tuple[float, float],
    amount: tuple[float, float],
    apply_minimum: bool,
    apply_maximum: bool,
) -> tuple[float, float]:
    minimum, maximum = current
    if apply_minimum:
        minimum += amount[0]
    if apply_maximum:
        maximum += amount[1]
    return minimum, maximum


def _combine_damage_layers(
    *,
    class_base: float,
    weapon_damage: tuple[float, float],
    equipped_item_damage: tuple[float, float],
    weapon_damage_bonus: float,
    weapon_damage_factor_relative: float,
    damage_absolute: tuple[float, float],
    damage_relative: tuple[float, float],
    pve_factor: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Apply the native weapon -> equipment -> global damage layers."""

    weapon_factor = max(0.0, 1.0 + weapon_damage_factor_relative)
    final_weapon = (
        (weapon_damage[0] + weapon_damage_bonus) * weapon_factor,
        (weapon_damage[1] + weapon_damage_bonus) * weapon_factor,
    )
    equipment = (
        final_weapon[0] + equipped_item_damage[0],
        final_weapon[1] + equipped_item_damage[1],
    )
    total = (
        (
            class_base + equipment[0] + damage_absolute[0]
        ) * max(0.0, 1.0 + damage_relative[0]) * pve_factor,
        (
            class_base + equipment[1] + damage_absolute[1]
        ) * max(0.0, 1.0 + damage_relative[1]) * pve_factor,
    )
    return equipment, total


def _calculate_vitality(
    database: StaticPlayerCombatDatabase,
    *,
    class_name: str,
    level: int,
    instructions: tuple[_Instruction, ...],
    variables: dict[str, float],
    equipped_item_health: float = 0.0,
) -> tuple[
    int, int, float, float, float, float, float, float, float
]:
    """Resolve maxima plus authored health and skill-resource recovery."""

    health_absolute = 0.0
    health_relative = 0.0
    health_set_multipliers = 1.0
    resource_absolute = 0.0
    resource_relative = 0.0
    regeneration_relative, regeneration_absolute, interval = (
        database.player_recovery(class_name)
    )
    base_regeneration_relative = regeneration_relative
    base_regeneration_absolute = regeneration_absolute
    regeneration_multiplier = 0.0
    (
        resource_regeneration_relative,
        resource_regeneration_absolute,
        resource_regeneration_interval,
        resource_regeneration_delay,
    ) = database.player_resource_recovery(class_name)
    for instruction in instructions:
        modifier = instruction.modifier
        kind = modifier.kind.casefold()
        if kind not in {
            "healthpoints",
            "maxhealthpoints",
            "maxskillresource",
            "healthpointsregeneration",
            "skillresourceregeneration",
        }:
            continue
        amount_range = _resolved_range(instruction, variables)
        if amount_range is None:
            continue
        amount = (float(amount_range[0]) + float(amount_range[1])) * 0.5
        operation = (
            modifier.arguments[1].casefold()
            if len(modifier.arguments) > 1
            else "absolute"
        )
        if kind == "healthpointsregeneration":
            if operation == "relative":
                # HealthPointsRegeneration::Apply (0x1408b88a4):
                # IncreaseRelative scales the relative-regeneration attribute;
                # ordinary relative modifiers add a max-health fraction.
                if "increaserelative" in _scopes(modifier.arguments):
                    regeneration_multiplier += amount
                else:
                    regeneration_relative += amount
            else:
                regeneration_absolute += amount
        elif kind == "skillresourceregeneration":
            if operation == "relative":
                resource_regeneration_relative += amount
            else:
                resource_regeneration_absolute += amount
        elif kind == "maxskillresource":
            if operation == "relative":
                resource_relative += amount
            else:
                resource_absolute += amount
        elif operation == "relative":
            # The Shadow Soul set's advertised +20% Maximum Health is a
            # final health multiplier. Keep it separate from ordinary
            # relative health bonuses, which are additive against the base
            # health pool; otherwise existing bonuses dilute the set's
            # stated 20% effect.
            source_set_id = str(
                getattr(modifier, "source_set_id", "")
            ).casefold()
            if (
                kind == "maxhealthpoints"
                and source_set_id.startswith(
                    "mercenary_souldestruction_"
                )
            ):
                health_set_multipliers *= max(0.0, 1.0 + amount)
            else:
                health_relative += amount
        else:
            health_absolute += amount
    maximum_health = max(
        1,
        int(round(
            (
                database.base_health(class_name, level)
                + max(0.0, float(equipped_item_health))
                + health_absolute
            )
            * max(0.0, 1.0 + health_relative)
            * health_set_multipliers
        )),
    )
    maximum_resource = max(
        1,
        int(round(
            (database.base_resource(class_name, level) + resource_absolute)
            * max(0.0, 1.0 + resource_relative)
        )),
    )
    return (
        maximum_health,
        maximum_resource,
        # The character sheet subtracts these class defaults. Passive healing
        # follows that displayed bonus; a zero regeneration stat cannot heal.
        max(0.0, regeneration_relative * max(0.0, 1.0 + regeneration_multiplier)
            - base_regeneration_relative),
        max(0.0, regeneration_absolute - base_regeneration_absolute),
        max(0.05, interval),
        resource_regeneration_relative,
        resource_regeneration_absolute,
        max(0.05, resource_regeneration_interval),
        max(0.0, resource_regeneration_delay),
    )


_ELEMENTAL_RESISTANCES = (
    "fire", "ice", "lightning", "darkmagic", "poison"
)


def _calculate_defenses_and_life_leech(
    database: StaticPlayerCombatDatabase,
    *,
    class_name: str,
    level: int,
    instructions: tuple[_Instruction, ...],
    variables: dict[str, float],
    weapon_category: str = "",
    has_offhand: bool = False,
    equipped_item_block: float = 0.0,
    equipped_item_armor: float = 0.0,
    equipped_item_resistances: tuple[tuple[str, float], ...] = (),
) -> tuple[
    float,
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
    tuple[tuple[float, frozenset[str]], ...],
    float,
    float,
    float,
]:
    """Resolve native armor/resistance ratings and scoped life leech."""

    base_armor, base_resistance_pairs = database.player_template_defenses.get(
        str(class_name or "").casefold(), (0.0, ())
    )
    base_resistances = dict(base_resistance_pairs)
    armor_absolute = max(0.0, float(base_armor)) + equipped_item_armor
    armor_relative = 0.0
    resistance_absolute = {
        name: max(0.0, float(base_resistances.get(name, 0.0)))
        for name in _ELEMENTAL_RESISTANCES
    }
    for name, value in equipped_item_resistances:
        resistance_absolute[name] += value
    resistance_relative = {name: 0.0 for name in _ELEMENTAL_RESISTANCES}
    life_leech_programs = []
    damage_reflection_fraction = 0.0
    block_rating_absolute = max(0.0, float(equipped_item_block))
    block_rating_relative = 0.0
    block_reduction_absolute = 0.0
    block_reduction_relative = 0.0

    for instruction in instructions:
        modifier = instruction.modifier
        kind = modifier.kind.casefold()
        if kind not in {
            "armor", "itemarmor", "resistance", "itemresistance",
            "lifeleech",
            "damagereflection",
            "block", "blockvalue",
            "smallweaponblock", "largeweaponblock", "offhandblock",
        }:
            continue
        amount_range = _resolved_range(instruction, variables)
        if amount_range is None:
            continue
        amount = (float(amount_range[0]) + float(amount_range[1])) * 0.5
        operation = (
            modifier.arguments[1].casefold()
            if len(modifier.arguments) > 1
            else "absolute"
        )
        scopes = _scopes(modifier.arguments)
        if kind == "lifeleech":
            life_leech_programs.append((
                max(0.0, amount),
                frozenset(
                    scope
                    for scope in scopes
                    if scope not in {"all", "allplayerskills", "skills"}
                ),
            ))
            continue
        if kind == "damagereflection":
            damage_reflection_fraction += max(0.0, amount)
            continue
        if kind in {
            "block", "blockvalue",
            "smallweaponblock", "largeweaponblock", "offhandblock",
        }:
            if (
                kind == "smallweaponblock"
                and weapon_category != "smallweapon"
            ):
                continue
            if (
                kind == "largeweaponblock"
                and weapon_category != "largeweapon"
            ):
                continue
            if kind == "offhandblock" and not has_offhand:
                continue
            is_reduction = "damagereduction" in scopes
            if is_reduction:
                if operation == "relative":
                    block_reduction_relative += amount
                else:
                    block_reduction_absolute += amount
            elif operation == "relative":
                block_rating_relative += amount
            else:
                block_rating_absolute += amount
            continue

        physical = kind in {"armor", "itemarmor"} or "physical" in scopes
        if physical:
            if operation == "relative":
                armor_relative += amount
            else:
                armor_absolute += amount

        selected_elements = {
            ("darkmagic" if scope == "andermagic" else scope)
            for scope in scopes
            if ("darkmagic" if scope == "andermagic" else scope)
            in _ELEMENTAL_RESISTANCES
        }
        meaningful_scopes = {
            scope
            for scope in scopes
            if scope not in {
                "levelmodifier", "resistancefactorabsolute",
                "resistancefactorrelative", "all",
            }
        }
        if (
            kind in {"resistance", "itemresistance"}
            and not selected_elements
            and not meaningful_scopes
        ):
            # An unscoped resistance instruction is the native all-element
            # form. Physical is kept on the separate armor curve.
            selected_elements = set(_ELEMENTAL_RESISTANCES)
        for element in selected_elements:
            if operation == "relative":
                resistance_relative[element] += amount
            else:
                resistance_absolute[element] += amount

    armor_rating = max(
        0.0, armor_absolute * max(0.0, 1.0 + armor_relative)
    )
    resistance_ratings = tuple(
        (
            element,
            max(
                0.0,
                resistance_absolute[element]
                * max(0.0, 1.0 + resistance_relative[element]),
            ),
        )
        for element in _ELEMENTAL_RESISTANCES
    )
    armor_threshold = max(
        1.0,
        database.progression_factor(level, "ArmorDamageReductionValue"),
    )
    resistance_threshold = max(
        1.0,
        database.progression_factor(
            level, "ResistanceDamageReductionValue"
        ),
    )
    multipliers = ((
        "physical",
        armor_threshold / (armor_threshold + armor_rating),
    ),) + tuple(
        (
            element,
            resistance_threshold / (resistance_threshold + rating),
        )
        for element, rating in resistance_ratings
    )
    block_rating = max(
        0.0,
        block_rating_absolute * max(0.0, 1.0 + block_rating_relative),
    )
    block_chance = min(
        database.maximum_block_chance,
        block_rating
        * database.progression_factor(level, "BlockChanceFactor"),
    )
    block_damage_reduction = min(
        database.maximum_block_damage_reduction,
        max(
            0.0,
            block_reduction_absolute
            * max(0.0, 1.0 + block_reduction_relative),
        ),
    )
    return (
        armor_rating,
        resistance_ratings,
        multipliers,
        tuple(life_leech_programs),
        max(0.0, damage_reflection_fraction),
        block_chance,
        block_damage_reduction,
    )


_PLAYER_COMBAT_STATS_CACHE_MAXIMUM = 1024
_player_combat_stats_cache: OrderedDict[tuple, PlayerCombatStats] = (
    OrderedDict()
)
_player_combat_stats_cache_lock = threading.Lock()


class _OwnerCombatStatsCache:
    """Small hot-path cache owned by one connected player session.

    ``InventoryInfo`` mutations in the production service are copy-on-write.
    Remembering the current object therefore makes the common hit path an
    identity check instead of rebuilding an equipment signature from every
    item in every bag. When a new inventory snapshot is published we build
    the structural signature once; bag-only, currency, and live HP/resource
    changes retain the already calculated equipment result.

    Modifier sources still participate in each entry key. Status entries and
    generated modifiers are immutable, so starting, stacking, stopping, or
    switching a talent/status effect cannot return a stale snapshot even if a
    caller omitted an explicit invalidation hook.
    """

    __slots__ = (
        "lock",
        "inventory_reference",
        "inventory_signature",
        "equipment_revision",
        "explicit_revision",
        "database",
        "database_identity",
        "database_check_after",
        "entries",
        "hits",
        "misses",
        "inventory_observations",
        "invalidations",
    )

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.inventory_reference = _UNOBSERVED_INVENTORY
        self.inventory_signature = None
        self.equipment_revision = 0
        self.explicit_revision = 0
        self.database = None
        self.database_identity = None
        self.database_check_after = 0.0
        self.entries: OrderedDict[tuple, PlayerCombatStats] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.inventory_observations = 0
        self.invalidations = 0


_UNOBSERVED_INVENTORY = object()
_OWNER_COMBAT_STATS_CACHE_ATTRIBUTE = "_gameplay_combat_stats_cache"
# Several stacking buffs multiply the distinct modifier states a player
# cycles through; at 16 entries they evicted each other mid-fight.
_OWNER_COMBAT_STATS_CACHE_MAXIMUM = 64
_OWNER_COMBAT_DATABASE_CHECK_SECONDS = 1.0
_owner_combat_stats_cache_install_lock = threading.Lock()


def _owner_combat_stats_cache(owner) -> _OwnerCombatStatsCache:
    current = getattr(owner, _OWNER_COMBAT_STATS_CACHE_ATTRIBUTE, None)
    if isinstance(current, _OwnerCombatStatsCache):
        return current
    candidate = _OwnerCombatStatsCache()
    with _owner_combat_stats_cache_install_lock:
        current = getattr(owner, _OWNER_COMBAT_STATS_CACHE_ATTRIBUTE, None)
        if isinstance(current, _OwnerCombatStatsCache):
            return current
        setattr(owner, _OWNER_COMBAT_STATS_CACHE_ATTRIBUTE, candidate)
        return candidate


def invalidate_player_combat_stats(
    owner, *, inventory_changed: bool = False
) -> None:
    """Invalidate one connected character's derived combat snapshot.

    Equipment/socket/talent/status mutation handlers call this at their
    authoritative commit point. ``inventory_changed`` additionally requests
    a fresh equipment observation and is useful for the rare code path that
    mutates an ``InventoryInfo`` in place. Ordinary current HP/resource
    updates deliberately do not invalidate derived stats.
    """

    cache = _owner_combat_stats_cache(owner)
    with cache.lock:
        cache.explicit_revision += 1
        cache.invalidations += 1
        cache.entries.clear()
        if inventory_changed:
            cache.inventory_reference = _UNOBSERVED_INVENTORY
            cache.inventory_signature = None


def player_combat_stats_cache_metrics(owner) -> dict[str, int]:
    """Return lock-safe counters for diagnostics and regression benchmarks."""

    cache = _owner_combat_stats_cache(owner)
    with cache.lock:
        return {
            "hits": int(cache.hits),
            "misses": int(cache.misses),
            "inventory_observations": int(cache.inventory_observations),
            "invalidations": int(cache.invalidations),
            "equipment_revision": int(cache.equipment_revision),
            "explicit_revision": int(cache.explicit_revision),
            "entries": len(cache.entries),
        }


def _combat_item_signature(item, slots) -> tuple:
    return (
        int(item.item_id),
        tuple(int(slot) for slot in slots),
        str(item.template_id),
        max(1, int(getattr(item, "item_level", 1))),
        max(0, int(getattr(item, "field_08", 0))),
        int(getattr(item, "field_18", 0)),
        max(0, int(getattr(item, "field_38", 0))),
        tuple(
            (
                float(getattr(enchantment, "value", 0.0)),
                int(getattr(enchantment, "kind", 0)),
                int(getattr(enchantment, "field_08", 0)),
                int(getattr(enchantment, "field_0c", 0)),
                str(getattr(enchantment, "template_id", "")),
            )
            for enchantment in getattr(item, "enchantments", ())
        ),
        tuple(
            str(template_id)
            for template_id in getattr(item, "socketed_templates", ())
        ),
    )


def _combat_inventory_signature(inventory) -> tuple:
    if inventory is None:
        return ()
    items_by_id = {
        int(item.item_id): item for item in getattr(inventory, "items", ())
    }
    return tuple(
        _combat_item_signature(items_by_id[item_id], slots)
        for item_id, slots in sorted(
            (
                int(raw_item_id),
                tuple(int(slot) for slot in raw_slots),
            )
            for raw_item_id, raw_slots in getattr(
                inventory, "equipment_slots", {}
            ).items()
            if int(raw_item_id) in items_by_id
        )
    )


# (id(source), id(database)) -> (source, database, signature) for frozen
# sources: every cast and impact re-keyed the same ~85 live entries.
_COMBAT_SIGNATURE_CACHE: dict[tuple[int, int], tuple[object, object, tuple]] = {}


def _combat_modifier_signature(
    source,
    database: StaticPlayerCombatDatabase | None = None,
) -> tuple:
    identity = (id(source), id(database))
    cached = _COMBAT_SIGNATURE_CACHE.get(identity)
    if cached is not None and cached[0] is source and cached[1] is database:
        return cached[2]
    signature = _uncached_combat_modifier_signature(source, database)
    parameters = getattr(type(source), "__dataclass_params__", None)
    if parameters is not None and parameters.frozen:
        if len(_COMBAT_SIGNATURE_CACHE) >= 65536:
            _COMBAT_SIGNATURE_CACHE.clear()
        _COMBAT_SIGNATURE_CACHE[identity] = (source, database, signature)
    return signature


def _uncached_combat_modifier_signature(
    source,
    database: StaticPlayerCombatDatabase | None = None,
) -> tuple:
    if hasattr(source, "kind") and hasattr(source, "arguments"):
        return (
            "modifier",
            str(source.kind),
            tuple(str(value) for value in source.arguments),
            str(getattr(source, "source_set_id", "")),
        )
    if hasattr(source, "effect_id"):
        effect_id = int(source.effect_id)
        occurrence = int(getattr(source, "field_18", 0))
        effective_stack = max(1, occurrence + 1)
        if database is not None:
            effective_stack = min(
                database.status_effect_max_stack_sizes.get(effect_id, 1),
                effective_stack,
            )
        return (
            "effect",
            effect_id,
            effective_stack,
            tuple(float(value) for value in getattr(source, "values", ())),
        )
    try:
        return ("effect-id", int(source))
    except (TypeError, ValueError):
        # Unknown future modifier carriers should remain correctness-first:
        # their complete representation participates in the cache key.
        return ("opaque", type(source).__qualname__, repr(source))


def calculate_player_combat_stats(
    database_path: str | Path,
    *,
    class_name: str,
    level: int,
    inventory,
    modifier_sources: tuple[object, ...] = (),
    cache_owner=None,
) -> PlayerCombatStats:
    """Calculate and structurally cache a player's current combat snapshot.

    Hit resolution calls this for damage, mitigation, regeneration, and life
    leech. Rebuilding the same equipped-item/status program for every impact
    made a packed AOE fight spend milliseconds per target and delayed both
    monster ticks and RakNet ACK handling. The immutable key contains every
    item/status field consumed below, so equipment, socket, stack, talent, or
    database-revision changes produce a miss without fragile manual
    invalidation hooks.
    """

    sources = tuple(modifier_sources)
    if cache_owner is not None:
        # A calculation captured before a concurrent inventory commit must
        # not move the owner's cache back to an obsolete object. It remains a
        # valid immutable snapshot for that cast, so use the structural cache
        # without publishing it as the current session generation.
        current_inventory = getattr(
            cache_owner, "gameplay_inventory", inventory
        )
        if current_inventory is inventory:
            return _calculate_owner_player_combat_stats(
                cache_owner,
                database_path,
                class_name=class_name,
                level=level,
                inventory=inventory,
                modifier_sources=sources,
            )

    database = prime_player_combat_database(database_path)
    cache_key = (
        str(database.database_path),
        tuple(database.database_revision),
        str(class_name).casefold(),
        max(1, int(level)),
        _combat_inventory_signature(inventory),
        tuple(
            _combat_modifier_signature(source, database)
            for source in sources
        ),
    )
    with _player_combat_stats_cache_lock:
        cached = _player_combat_stats_cache.get(cache_key)
        if cached is not None:
            _player_combat_stats_cache.move_to_end(cache_key)
            return cached
    calculated = _calculate_player_combat_stats(
        database,
        class_name=class_name,
        level=level,
        inventory=inventory,
        modifier_sources=sources,
    )
    with _player_combat_stats_cache_lock:
        existing = _player_combat_stats_cache.get(cache_key)
        if existing is not None:
            _player_combat_stats_cache.move_to_end(cache_key)
            return existing
        _player_combat_stats_cache[cache_key] = calculated
        while len(_player_combat_stats_cache) > _PLAYER_COMBAT_STATS_CACHE_MAXIMUM:
            _player_combat_stats_cache.popitem(last=False)
    return calculated


def _calculate_owner_player_combat_stats(
    owner,
    database_path: str | Path,
    *,
    class_name: str,
    level: int,
    inventory,
    modifier_sources: tuple[object, ...],
) -> PlayerCombatStats:
    """Resolve one session snapshot without structural work on cache hits."""

    cache = _owner_combat_stats_cache(owner)
    now = time.perf_counter()

    # Preserve the old database-replacement correctness guarantee without a
    # Path.stat() syscall per projectile impact. At most once per second per
    # active character is sufficient for an immutable production content DB.
    with cache.lock:
        check_database = now >= cache.database_check_after
    if check_database:
        database = prime_player_combat_database(database_path)
        database_identity = (
            str(database.database_path), tuple(database.database_revision)
        )
        with cache.lock:
            if cache.database_identity != database_identity:
                cache.database_identity = database_identity
                cache.entries.clear()
            cache.database = database
            cache.database_check_after = (
                now + _OWNER_COMBAT_DATABASE_CHECK_SECONDS
            )

    with cache.lock:
        if cache.inventory_reference is not inventory:
            inventory_signature = _combat_inventory_signature(inventory)
            cache.inventory_observations += 1
            if inventory_signature != cache.inventory_signature:
                cache.inventory_signature = inventory_signature
                cache.equipment_revision += 1
                cache.entries.clear()
            cache.inventory_reference = inventory

        # No equipped-item traversal occurs here on the ordinary hit path.
        # Effect tuples are intentionally structural: a runtime stack/value
        # refresh must not reuse the previous calculated modifier program.
        source_signature = tuple(
            _combat_modifier_signature(source, cache.database)
            for source in modifier_sources
        )
        cache_key = (
            cache.database_identity,
            cache.equipment_revision,
            cache.explicit_revision,
            str(class_name).casefold(),
            max(1, int(level)),
            source_signature,
        )
        cached = cache.entries.get(cache_key)
        if cached is not None:
            cache.entries.move_to_end(cache_key)
            cache.hits += 1
            return cached
        cache.misses += 1
        generation = (
            cache.equipment_revision,
            cache.explicit_revision,
            cache.database_identity,
        )

    # Misses retain the original full structural cache as a correctness
    # fallback and to share immutable snapshots between equivalent sessions.
    calculated = calculate_player_combat_stats(
        database_path,
        class_name=class_name,
        level=level,
        inventory=inventory,
        modifier_sources=modifier_sources,
    )

    with cache.lock:
        current_generation = (
            cache.equipment_revision,
            cache.explicit_revision,
            cache.database_identity,
        )
        if current_generation != generation:
            return calculated
        existing = cache.entries.get(cache_key)
        if existing is not None:
            cache.entries.move_to_end(cache_key)
            return existing
        cache.entries[cache_key] = calculated
        while len(cache.entries) > _OWNER_COMBAT_STATS_CACHE_MAXIMUM:
            cache.entries.popitem(last=False)
    return calculated


def _calculate_movement(
    database, class_name, instructions, variables, *, item_programs=(),
):
    """Item-local speed, capped actor movement, then the bonus channel."""
    base = database.player_template_movement.get(class_name.casefold(), 5.0)
    absolute = relative = bonus = 0.0
    for program in item_programs:
        item_absolute = item_relative = 0.0
        for instruction in program:
            modifier = instruction.modifier
            if (
                modifier.kind.casefold() != "itemspeed"
                or not _scopes(modifier.arguments).intersection({"movement", "all"})
            ):
                continue
            amount = _resolved_range(instruction, variables)
            if amount is None:
                continue
            value = sum(amount) / 2.0
            operation = modifier.arguments[1].casefold()
            if operation == "absolute":
                item_absolute += value
            elif operation == "relative":
                item_relative += value
        # Movement gems and base enchantments belong to their containing
        # item. Its relative lines cannot scale class speed or other items.
        absolute += item_absolute * max(0.0, 1.0 + item_relative)
    for instruction in instructions:
        modifier = instruction.modifier
        if modifier.kind.casefold() != "speed":
            continue
        amount = _resolved_range(instruction, variables)
        if amount is None:
            continue
        value = sum(amount) / 2.0
        scopes = _scopes(modifier.arguments)
        operation = modifier.arguments[1].casefold()
        if scopes.intersection({"movement", "all"}):
            if operation == "absolute":
                absolute += value
            elif operation == "relative":
                relative += value
        # Speed::Apply ignores relative MovementBonus modifiers.
        if "movementbonus" in scopes and operation == "absolute":
            bonus += value
    lower, upper = database.movement_speed_limits
    ordinary = max(lower, min(upper, (base + absolute) * max(0.0, 1.0 + relative)))
    # Keep the raw bonus sum: an admin contribution must be removed before
    # testing the client's total speed against its wire limit.
    return base, ordinary, bonus


def _calculate_attack_speed(
    database, class_name, instructions, variables, item_groups, allocations,
):
    """Native item speed plus scoped weapon bonuses, then actor Speed."""
    actor_absolute = actor_relative = 0.0
    weapon_bonus = {"smallweapon": 0.0, "largeweapon": 0.0}
    weapon_factor = {"smallweapon": 0.0, "largeweapon": 0.0}
    for instruction in instructions:
        modifier = instruction.modifier
        kind = modifier.kind.casefold()
        scopes = _scopes(modifier.arguments)
        if kind == "speed":
            if not scopes.intersection({"skills", "allplayerskills", "all"}):
                continue
        elif kind not in {"weaponspeedbonus", "weaponspeedfactor"}:
            continue
        amount = _resolved_range(instruction, variables)
        if amount is None:
            continue
        value = sum(amount) / 2.0
        relative = modifier.arguments[1].casefold() == "relative"
        if kind == "speed":
            if relative:
                actor_relative += value
            else:
                actor_absolute += value
        else:
            targets = scopes.intersection(weapon_bonus) or set(weapon_bonus)
            bucket = weapon_bonus if kind == "weaponspeedbonus" else weapon_factor
            for target in targets:
                bucket[target] += value
    item_total = 0.0
    for item, _definition, program in item_groups:
        absolute = relative = 0.0
        for instruction in program:
            modifier = instruction.modifier
            if modifier.kind.casefold() != "itemspeed" or not _scopes(
                modifier.arguments
            ).intersection({"skills", "allplayerskills", "all"}):
                continue
            amount = _resolved_range(instruction, variables)
            if amount is None:
                continue
            value = sum(amount) / 2.0
            if modifier.arguments[1].casefold() == "relative":
                relative += value
            else:
                absolute += value
        speed = absolute * max(0.0, 1.0 + relative)
        slots = allocations.get(int(item.item_id), ())
        if 5 in slots:
            category = "largeweapon" if 6 in slots else "smallweapon"
            speed = (speed + weapon_bonus[category]) * max(0.0, 1.0 + weapon_factor[category])
        item_total += speed
    minimum = database.player_skill_speed_minimum.get(class_name.casefold(), 0.5)
    return max(minimum, (1.0 + item_total + actor_absolute) * max(0.0, 1.0 + actor_relative))


@lru_cache(maxsize=4)
def equip_item_level_offset(database_path: str) -> int:
    """_Globals.EquipItemLevelOffset (45): a character wears items up to its
    level plus this (a level 100 wears its level 145 gear)."""

    try:
        with sqlite3.connect(f"file:{Path(database_path).as_posix()}?mode=ro", uri=True) as database:
            row = database.execute("SELECT EquipItemLevelOffset FROM _Globals LIMIT 1").fetchone()
    except sqlite3.Error:
        return 45
    return int(row[0]) if row and row[0] is not None else 45


def _calculate_player_combat_stats(
    database: StaticPlayerCombatDatabase,
    *,
    class_name: str,
    level: int,
    inventory,
    modifier_sources: tuple[object, ...] = (),
) -> PlayerCombatStats:
    """Uncached implementation for one immutable structural snapshot."""

    class_base = database.base_damage(class_name, level)
    base_health = int(round(database.base_health(class_name, level)))
    base_resource = int(round(database.base_resource(class_name, level)))
    base_regeneration, base_regeneration_absolute, regeneration_interval = (
        database.player_recovery(class_name)
    )
    if inventory is None:
        movement_instructions = _status_instructions(database, modifier_sources, level=level)
        movement = _calculate_movement(
            database, class_name, movement_instructions,
            _apply_variable_programs(movement_instructions, database.actor_variables),
        )
        (
            armor_rating,
            resistance_ratings,
            incoming_damage_multipliers,
            life_leech_programs,
            damage_reflection_fraction,
            block_chance,
            block_damage_reduction,
        ) = _calculate_defenses_and_life_leech(
            database,
            class_name=class_name,
            level=level,
            instructions=(),
            variables=database.actor_variables,
        )
        return PlayerCombatStats(
            minimum_damage=class_base,
            maximum_damage=class_base,
            class_base_damage=class_base,
            equipment_minimum_damage=0.0,
            equipment_maximum_damage=0.0,
            skill_modifier_sources=(),
            base_movement_speed=movement[0],
            movement_speed=movement[1],
            movement_bonus_speed=movement[2],
            critical_damage_multiplier=database.base_critical_damage,
            maximum_health=base_health,
            maximum_resource=base_resource,
            health_regeneration_fraction=0.0,
            health_regeneration_absolute=0.0,
            health_regeneration_interval=regeneration_interval,
            resource_regeneration_fraction=(
                database.player_resource_recovery(class_name)[0]
            ),
            resource_regeneration_absolute=(
                database.player_resource_recovery(class_name)[1]
            ),
            resource_regeneration_interval=(
                database.player_resource_recovery(class_name)[2]
            ),
            resource_regeneration_delay=(
                database.player_resource_recovery(class_name)[3]
            ),
            armor_rating=armor_rating,
            resistance_ratings=resistance_ratings,
            incoming_damage_multipliers=incoming_damage_multipliers,
            life_leech_programs=life_leech_programs,
            damage_reflection_fraction=damage_reflection_fraction,
            block_chance=block_chance,
            block_damage_reduction=block_damage_reduction,
        )

    items_by_id = {
        int(item.item_id): item for item in getattr(inventory, "items", ())
    }
    equipped = []
    allocations = {}
    # Slots 25..27 are the summoned Shadow Soul's amulet, cloak, and belt.
    # They stay equipped in InventoryInfo so ItemSetDatabase can count the
    # companion set, but their individual item stats belong to the companion,
    # not the player. In particular, counting old mercenary gear's
    # HealthPoints here inflated the player's pre-set max HP and made the
    # 20% MaxHealthPoints set tier appear to grant less than 20% when the new
    # set replaced those pieces.
    mercenary_equipment_slots = {25, 26, 27}
    # The secondary essence stays equipped but does not act: only the active
    # slot's essence supplies its damage factor and ammunition type here, and
    # therefore to every cast that reads ``skill_modifier_sources``.
    for item_id, allocation in active_equipment_slots(inventory).items():
        if mercenary_equipment_slots.intersection(allocation):
            continue
        item = items_by_id.get(item_id)
        definition = (
            database.items.get(str(item.template_id))
            if item is not None
            else None
        )
        if item is None or definition is None:
            continue
        # Above level + EquipItemLevelOffset an item does not act: its
        # ratings, converted at the wearer's low level, reached every cap.
        # FAILURE: a level 7 in level 145 gear struck 196328 (80% critical
        #   hits) and took 5% of a level 100's blows.
        # UNVERIFIED: the original refuses to equip it (DSOR_Client does);
        #   ignoring an already worn one is this server's choice.
        if int(getattr(item, "item_level", 0) or 0) > int(level) + equip_item_level_offset(str(database.database_path)):
            continue
        allocations[item_id] = allocation
        equipped.append((item, definition))

    all_item_instructions = []
    item_instruction_groups = []
    for item, definition in equipped:
        instructions = _cached_item_instructions(database, item)
        item_instruction_groups.append((item, definition, instructions))
        all_item_instructions.extend(instructions)

    active_instructions = _status_instructions(
        database,
        tuple(modifier_sources),
        level=max(1, int(level)),
    )
    combined = tuple(all_item_instructions) + active_instructions
    variables = _apply_variable_programs(
        combined, database.actor_variables
    )
    movement = _calculate_movement(
        database, class_name, combined, variables,
        item_programs=tuple(
            instructions for _item, _definition, instructions
            in item_instruction_groups
        ),
    )
    skill_sources = tuple(
        _materialized_skill_source(instruction, variables)
        for instruction in all_item_instructions
    )

    weapon_damage = (0.0, 0.0)
    equipped_item_damage = (0.0, 0.0)
    equipped_item_health = 0.0
    equipped_item_block = 0.0
    equipped_item_armor = 0.0
    equipped_item_critical = 0.0
    equipped_item_resistances = {name: 0.0 for name in _ELEMENTAL_RESISTANCES}
    global_instructions = []
    weapon_category = ""
    for item, definition, instructions in item_instruction_groups:
        slots = allocations.get(int(item.item_id), ())
        if 5 in slots:
            weapon_category = (
                "smallweapon"
                if definition.category.casefold() == "smallweapon"
                else "largeweapon"
            )
        item_level = max(1, int(getattr(item, "item_level", 1)))
        level_scale = (
            database.progression_factor(
                item_level, "DamageFactorAbsolute"
            )
            if definition.scale_with_level else 1.0
        )
        item_minimum = definition.minimum_damage * level_scale
        item_maximum = definition.maximum_damage * level_scale
        absolute = (0.0, 0.0)
        relative = (0.0, 0.0)
        item_health_absolute = 0.0
        item_health_relative = 0.0
        item_block_absolute = 0.0
        item_block_relative = 0.0
        rating_absolute = dict.fromkeys(("critical", "physical", "all_resistances", *_ELEMENTAL_RESISTANCES), 0.0)
        rating_relative = dict(rating_absolute)
        for instruction in instructions:
            modifier = instruction.modifier
            modifier_kind = modifier.kind.casefold()
            if modifier_kind in {"itemcriticalvalue", "itemarmor", "itemresistance"}:
                amount = _resolved_range(instruction, variables)
                if amount is None:
                    continue
                if modifier_kind == "itemcriticalvalue":
                    targets = {"critical"}
                elif modifier_kind == "itemarmor":
                    targets = {"physical"}
                else:
                    scopes = _scopes(modifier.arguments)
                    targets = {
                        "darkmagic" if scope == "andermagic" else scope
                        for scope in scopes
                    }.intersection({"physical", *_ELEMENTAL_RESISTANCES})
                    if not targets and not scopes.difference({
                        "all", "levelmodifier", "resistancefactorabsolute", "resistancefactorrelative",
                    }):
                        targets = {"all_resistances"}
                bucket = (rating_relative if modifier.arguments[1].casefold() == "relative"
                          else rating_absolute)
                for target in targets:
                    bucket[target] += sum(amount) / 2.0
                continue
            if modifier_kind == "itemblockvalue":
                amount = _resolved_range(instruction, variables)
                if amount is not None:
                    value = sum(amount) / 2.0
                    if (len(modifier.arguments) > 1
                            and modifier.arguments[1].casefold() == "relative"):
                        item_block_relative += value
                    else:
                        item_block_absolute += value
                continue
            if modifier_kind == "itemhealthpoints":
                amount = _resolved_range(instruction, variables)
                if amount is None:
                    continue
                value = sum(amount) / 2.0
                operation = (
                    modifier.arguments[1].casefold()
                    if len(modifier.arguments) > 1
                    else "absolute"
                )
                if operation == "relative":
                    item_health_relative += value
                else:
                    if any(
                        str(argument).casefold() == "useclasshitpoints"
                        for argument in modifier.arguments[2:]
                    ):
                        value *= database.class_health_factor(class_name)
                    item_health_absolute += value
                continue
            if modifier_kind != "itemdamage":
                global_instructions.append(instruction)
                continue
            amount = _resolved_range(instruction, variables)
            if amount is None:
                continue
            operation = (
                modifier.arguments[1].casefold()
                if len(modifier.arguments) > 1
                else "absolute"
            )
            target = relative if operation == "relative" else absolute
            target = _relative_total(target, amount, True, True)
            if operation == "relative":
                relative = target
            else:
                absolute = target
        item_minimum = (item_minimum + absolute[0]) * max(
            0.0, 1.0 + relative[0]
        )
        item_maximum = (item_maximum + absolute[1]) * max(
            0.0, 1.0 + relative[1]
        )
        # ItemHealthPoints is a GameItem attribute. Relative lines modify
        # only the absolute health owned by this item; they must never scale
        # class base health, Wisdom, sockets on other items, or global buffs.
        equipped_item_health += item_health_absolute * max(
            0.0, 1.0 + item_health_relative
        )
        # ItemBlockValue is local to this item, before global rune, Wisdom
        # and status-effect block modifiers are combined.
        equipped_item_block += item_block_absolute * max(
            0.0, 1.0 + item_block_relative
        )
        item_ratings = {
            name: value * max(0.0, 1.0 + rating_relative[name])
            for name, value in rating_absolute.items()
        }
        equipped_item_critical += item_ratings["critical"]
        equipped_item_armor += item_ratings["physical"]
        for name in _ELEMENTAL_RESISTANCES:
            equipped_item_resistances[name] += item_ratings[name] + item_ratings["all_resistances"]
        if 5 in slots:
            weapon_damage = (
                weapon_damage[0] + item_minimum,
                weapon_damage[1] + item_maximum,
            )
            # A two-handed weapon owns both hand slots. One-handed weapons
            # own slot 5 while an independent offhand occupies slot 6.
            weapon_category = (
                "largeweapon" if 6 in slots else "smallweapon"
            )
        else:
            equipped_item_damage = (
                equipped_item_damage[0] + item_minimum,
                equipped_item_damage[1] + item_maximum,
            )

    # Portable ItemDamage is the weapon-local increased-damage layer. Current
    # retail data emits it from item instances; retaining this branch also
    # handles future conditional/status-owned ItemDamage programs correctly.
    active_item_absolute = (0.0, 0.0)
    active_item_relative = (0.0, 0.0)
    for instruction in active_instructions:
        if instruction.modifier.kind.casefold() != "itemdamage":
            global_instructions.append(instruction)
            continue
        amount = _resolved_range(instruction, variables)
        if amount is None:
            continue
        operation = (
            instruction.modifier.arguments[1].casefold()
            if len(instruction.modifier.arguments) > 1
            else "absolute"
        )
        if operation == "relative":
            active_item_relative = _relative_total(
                active_item_relative, amount, True, True
            )
        else:
            active_item_absolute = _relative_total(
                active_item_absolute, amount, True, True
            )
    weapon_damage = (
        (weapon_damage[0] + active_item_absolute[0])
        * max(0.0, 1.0 + active_item_relative[0]),
        (weapon_damage[1] + active_item_absolute[1])
        * max(0.0, 1.0 + active_item_relative[1]),
    )

    damage_absolute = (0.0, 0.0)
    damage_relative = (0.0, 0.0)
    weapon_bonus = 0.0
    weapon_factor_relative = 0.0
    pve_factor_absolute = 0.0
    pve_factor_relative = 0.0
    critical_value_absolute = equipped_item_critical
    critical_value_relative = 0.0
    critical_damage_absolute = 0.0
    critical_damage_relative = 0.0
    for instruction in global_instructions:
        modifier = instruction.modifier
        kind = modifier.kind.casefold()
        amount = _resolved_range(instruction, variables)
        if amount is None:
            continue
        operation = (
            modifier.arguments[1].casefold()
            if len(modifier.arguments) > 1
            else "absolute"
        )
        if kind == "damage":
            apply_minimum, apply_maximum = _damage_scope(
                _scopes(modifier.arguments)
            )
            if operation == "relative":
                damage_relative = _relative_total(
                    damage_relative,
                    amount,
                    apply_minimum,
                    apply_maximum,
                )
            else:
                damage_absolute = _relative_total(
                    damage_absolute,
                    amount,
                    apply_minimum,
                    apply_maximum,
                )
        elif kind == "damagefactorpve":
            if operation == "relative":
                pve_factor_relative += sum(amount) / 2.0
            else:
                pve_factor_absolute += sum(amount) / 2.0
        elif (
            kind in {"criticalvalue", "itemcriticalvalue"}
            or (
                kind == "critical"
                and "rating" in _scopes(modifier.arguments)
            )
        ):
            value = sum(amount) / 2.0
            if operation == "relative":
                critical_value_relative += value
            else:
                critical_value_absolute += value
        elif (
            kind == "critical"
            and "damage" in _scopes(modifier.arguments)
        ):
            value = sum(amount) / 2.0
            if operation == "relative":
                critical_damage_relative += value
            else:
                critical_damage_absolute += value
        elif kind in {
            "smallweaponcritical", "largeweaponcritical", "offhandcritical",
        }:
            if (
                kind == "smallweaponcritical"
                and weapon_category != "smallweapon"
            ):
                continue
            if (
                kind == "largeweaponcritical"
                and weapon_category != "largeweapon"
            ):
                continue
            if (
                kind == "offhandcritical"
                and not any(6 in slots for slots in allocations.values())
            ):
                continue
            value = sum(amount) / 2.0
            if operation == "relative":
                critical_damage_relative += value
            else:
                critical_damage_absolute += value
        elif kind in {"weapondamagebonus", "weapondamagefactor"}:
            scopes = _scopes(modifier.arguments)
            weapon_scopes = scopes.intersection(
                {"smallweapon", "largeweapon"}
            )
            if weapon_scopes and weapon_category not in weapon_scopes:
                continue
            value = sum(amount) / 2.0
            if kind == "weapondamagebonus":
                weapon_bonus += value
            elif operation == "relative":
                weapon_factor_relative += value

    pve_factor = max(
        0.0,
        (1.0 + pve_factor_absolute) * (1.0 + pve_factor_relative),
    )
    equipment, total = _combine_damage_layers(
        class_base=class_base,
        weapon_damage=weapon_damage,
        equipped_item_damage=equipped_item_damage,
        weapon_damage_bonus=weapon_bonus,
        weapon_damage_factor_relative=weapon_factor_relative,
        damage_absolute=damage_absolute,
        damage_relative=damage_relative,
        pve_factor=pve_factor,
    )
    equipment_minimum, equipment_maximum = equipment
    minimum, maximum = total
    minimum = max(1.0, minimum)
    maximum = max(minimum, maximum)
    critical_value = max(
        0.0,
        critical_value_absolute
        * max(0.0, 1.0 + critical_value_relative),
    )
    critical_hit_chance = min(
        database.maximum_critical_chance,
        critical_value
        * database.progression_factor(level, "CriticalChanceFactor"),
    )
    critical_damage_multiplier = min(
        database.maximum_critical_damage,
        max(
            1.0,
            (database.base_critical_damage + critical_damage_absolute)
            * max(0.0, 1.0 + critical_damage_relative),
        ),
    )
    (
        maximum_health,
        maximum_resource,
        health_regeneration_fraction,
        health_regeneration_absolute,
        health_regeneration_interval,
        resource_regeneration_fraction,
        resource_regeneration_absolute,
        resource_regeneration_interval,
        resource_regeneration_delay,
    ) = _calculate_vitality(
        database,
        class_name=class_name,
        level=level,
        instructions=combined,
        variables=variables,
        equipped_item_health=equipped_item_health,
    )
    (
        armor_rating,
        resistance_ratings,
        incoming_damage_multipliers,
        life_leech_programs,
        damage_reflection_fraction,
        block_chance,
        block_damage_reduction,
    ) = _calculate_defenses_and_life_leech(
        database,
        class_name=class_name,
        level=level,
        instructions=tuple(global_instructions),
        variables=variables,
        weapon_category=weapon_category,
        has_offhand=any(6 in slots for slots in allocations.values()),
        equipped_item_block=equipped_item_block,
        equipped_item_armor=equipped_item_armor,
        equipped_item_resistances=tuple(equipped_item_resistances.items()),
    )
    return PlayerCombatStats(
        minimum_damage=minimum,
        maximum_damage=maximum,
        class_base_damage=class_base,
        equipment_minimum_damage=equipment_minimum,
        equipment_maximum_damage=equipment_maximum,
        skill_modifier_sources=skill_sources,
        base_movement_speed=movement[0],
        movement_speed=movement[1],
        movement_bonus_speed=movement[2],
        critical_hit_chance=critical_hit_chance,
        critical_rating=critical_value,
        attack_speed=_calculate_attack_speed(
            database, class_name, combined, variables, item_instruction_groups, allocations,
        ),
        critical_damage_multiplier=critical_damage_multiplier,
        maximum_health=maximum_health,
        maximum_resource=maximum_resource,
        health_regeneration_fraction=health_regeneration_fraction,
        health_regeneration_absolute=health_regeneration_absolute,
        health_regeneration_interval=health_regeneration_interval,
        resource_regeneration_fraction=resource_regeneration_fraction,
        resource_regeneration_absolute=resource_regeneration_absolute,
        resource_regeneration_interval=resource_regeneration_interval,
        resource_regeneration_delay=resource_regeneration_delay,
        armor_rating=armor_rating,
        resistance_ratings=resistance_ratings,
        incoming_damage_multipliers=incoming_damage_multipliers,
        life_leech_programs=life_leech_programs,
        damage_reflection_fraction=damage_reflection_fraction,
        block_chance=block_chance,
        block_damage_reduction=block_damage_reduction,
        mount_duration_scale=_status_group_duration_scale(
            combined, variables, "mount"
        ),
    )


__all__ = [
    "CombatModifier",
    "PlayerCombatStats",
    "StaticPlayerCombatDatabase",
    "calculate_player_combat_stats",
    "invalidate_player_combat_stats",
    "player_combat_stats_cache_metrics",
    "prime_player_combat_database",
]
