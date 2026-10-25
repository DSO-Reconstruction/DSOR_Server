"""Typed talent-selection request codec and database-backed validation."""

from __future__ import annotations

from dso_server.protocol.command_ids import command_id
from dataclasses import dataclass
import math
from pathlib import Path
import re
import sqlite3

from dso_server.persistence import readonly_db
from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.gameplay_protocol import StatusEffectEntry


SELECT_TALENT_COMMAND = command_id("Talents::SelectTalentCommand", 0x0117)
SELECT_TALENT_PRESELECTION_COMMAND = command_id("Talents::EnableTalentPreselectionCommand", 0x0119)
RESET_TALENT_PRESELECTION_COMMAND = command_id("Talents::ResetTalentsCommand", 0x011E)
RESET_TALENT_CATEGORY_COMMAND = command_id("Talents::ResetTalentCategoryCommand", 0x011F)
DESELECT_TALENT_COMMAND = command_id("Talents::DeselectTalentCommand", 0x0118)
UNLOCK_SKILL_TALENT_PRESELECTION_COMMAND = command_id(
    "Talents::UnlockSkillTalentPreselectionCommand", 0x011C
)
TALENT_CATEGORY_NAMES = ("Skill", "Knowledge", "Group", "Fame")
TALENT_CATEGORY_PRESELECTION_COUNTS = (8, 4, 4, 1)
SKILL_TALENT_PRESELECTION_COUNT = TALENT_CATEGORY_PRESELECTION_COUNTS[0]
# Presets below the membership's NumTalentSets are free.  Skill presets 4..7
# (4 = the most any privilege grants) are bought one after the other with
# Andermant: UnlockSkillTalentPreselection, priced by
# _Globals.UnlockPreselectionCost.  ClientTalentManager applies the same rule
# to its preset buttons.
SKILL_TALENT_BOUGHT_PRESELECTION_FIRST = 4
SKILL_TALENT_UNLOCKABLE_PRESELECTIONS = (
    SKILL_TALENT_PRESELECTION_COUNT - SKILL_TALENT_BOUGHT_PRESELECTION_FIRST
)
# _Template_Privileges row of each membership tier (inactive = NoPrivileges).
MEMBERSHIP_PRIVILEGES = {1: "Premium", 2: "Deluxe"}
# Switching a Skill/Knowledge/Group preset outside a hub puts this status
# effect (groups SkillPreset/KnowledgePreset/GroupPreset, 300 s) on the
# player; the client disables the other preset buttons while it runs.  Honor
# has a single preset and no cooldown.
TALENT_PRESET_COOLDOWN_EFFECTS = (
    "SkillPresetCooldown",
    "KnowledgePresetCooldown",
    "GroupPresetCooldown",
)
# The status effect whose TalentRespecCost:$0,relative scales respec prices.
TALENT_RESPEC_COST_EFFECT = "talent_reset_costs_buff"
# The Skills & Talents window's Wisdom trader and the trait that unlocks it.
WISDOM_TALENT_SHOP_ID = "skilltree_wisdom_trader"
WISDOM_TALENT_SHOP_TALENT = "trait_wisdom_all_dungeon_shop"
# Skill-talent layouts are zero based.  The first layout is 0 and the second
# layout selected in the 2026-09-09 retail trace is 1.
ACTIVE_SKILL_TALENT_PRESELECTION = 0
# Retail sends one rank per click, but the talent window's bulk allocation
# sends the whole batch as a single request.  The per-talent maximum rank is
# the real bound; this only rejects absurd uint32 values before the handler
# walks the per-rank cost table.
MAX_TALENT_RANK_DELTA = 255


@dataclass(frozen=True)
class SelectTalent:
    talent_id: str
    rank_delta: int

    @property
    def target_rank(self) -> int:
        """Compatibility alias for older inspection tools."""

        return self.rank_delta

    @property
    def preselection(self) -> int:
        """Deprecated compatibility alias.

        The trailing uint32 is the number of ranks requested (retail sends
        one per click), not the active preset.  Preset selection is carried
        exclusively by command ``0x0119``.
        """

        return self.rank_delta


@dataclass(frozen=True)
class SelectTalentPreselection:
    preselection: int
    category: int


@dataclass(frozen=True)
class ResetTalentPreselection:
    category: int
    preselection: int


@dataclass(frozen=True)
class ResetTalentCategory:
    category: int
    category_id: str
    preselection: int


@dataclass(frozen=True)
class DeselectTalent:
    talent_id: str


@dataclass(frozen=True)
class UnlockSkillTalentPreselection:
    # The bought Skill preset count once this purchase is applied: the client
    # sends its current count plus one, whichever button was clicked.
    unlocked: int


def talent_preselection_available(
    category: int,
    preselection: int,
    *,
    talent_sets: int,
    unlocked_skill_presets: int,
) -> bool:
    """Return whether a preset may be switched to (the client's rule)."""

    category = int(category)
    preselection = int(preselection)
    if not 0 <= category < len(TALENT_CATEGORY_PRESELECTION_COUNTS):
        return False
    if not 0 <= preselection < TALENT_CATEGORY_PRESELECTION_COUNTS[category]:
        return False
    if category == 3 or preselection < int(talent_sets):
        return True
    return category == 0 and (
        SKILL_TALENT_BOUGHT_PRESELECTION_FIRST
        <= preselection
        < SKILL_TALENT_BOUGHT_PRESELECTION_FIRST + int(unlocked_skill_presets)
    )


def talent_respec_cost(
    *,
    spent_points: int,
    cost_per_point: int,
    level_multiplier: float,
    factor: float = 1.0,
) -> int:
    """Gold (copper) price of a respec, PlayerTalentModule's formula.

    floor(VCRespecCostsPerPoint * RespecVCCostMultiplier[level] * spent *
    TalentRespecCostFactor + 0.5); above 999999 it is rounded down to a
    multiple of 10000.  Honor resets are free and never priced here.
    """

    cost = max(0, math.floor(
        int(cost_per_point)
        * float(level_multiplier)
        * int(spent_points)
        * float(factor)
        + 0.5
    ))
    if cost > 999999:
        cost = int(cost * 0.0001) * 10000
    return int(cost)


@dataclass(frozen=True)
class SkillTalentDefinition:
    talent_id: str
    class_name: str
    unlock_level: int
    max_rank: int
    unlock_cost: int
    cost_per_rank: int
    cost_multiplier: float
    skill_id: str
    base_status_effect_id: str
    stack_status_effect_id: str
    group_id: str
    selection_kind: str
    category_id: int = 0

    def point_cost_for_rank(self, target_rank: int) -> int:
        target_rank = int(target_rank)
        if target_rank <= 0 or target_rank > self.max_rank:
            raise ValueError(
                f"talent rank {target_rank} is outside 1..{self.max_rank}"
            )
        if target_rank == 1:
            return max(0, self.unlock_cost)
        multiplier = 1.0 + self.cost_multiplier * (target_rank - 2)
        return max(0, int(round(self.cost_per_rank * multiplier)))

    def status_effect_for_rank(self, target_rank: int) -> str:
        if target_rank > 1 and self.stack_status_effect_id:
            return self.stack_status_effect_id
        return self.base_status_effect_id


@dataclass(frozen=True)
class SkillTalentModifier:
    """One generated modifier instruction owned by a talent StatusEffect."""

    kind: str
    arguments: tuple[str, ...]

    @property
    def skill_status_effect_scope(self) -> str | None:
        if self.kind != "SkillStatusEffect" or not self.arguments:
            return None
        operation = self.arguments[0]
        if operation.endswith("UE"):
            return "player"
        if operation.endswith("VE"):
            return "victim"
        if operation.endswith("LE"):
            return "location"
        return None


def decode_select_talent(
    payload: bytes, bit_length: int | None = None
) -> SelectTalent:
    """Decode client command ``0x0117`` (talent id + rank delta uint32)."""

    bit_length = len(payload) * 8 if bit_length is None else int(bit_length)
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x8B:
        raise ValueError("SelectTalent does not begin with client marker 0x8b")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != SELECT_TALENT_COMMAND:
        raise ValueError(
            f"expected SelectTalent command 0x{SELECT_TALENT_COMMAND:04x}, "
            f"got 0x{command_id:04x}"
        )
    request = SelectTalent(reader.read_string(), reader.read_uint32_le())
    if not request.talent_id:
        raise ValueError("SelectTalent requires a talent id")
    if not 1 <= request.rank_delta <= MAX_TALENT_RANK_DELTA:
        raise ValueError(
            f"SelectTalent rank delta {request.rank_delta} is outside "
            f"1..{MAX_TALENT_RANK_DELTA}"
        )
    if reader.remaining:
        raise ValueError(
            f"SelectTalent contains {reader.remaining} trailing bit(s)"
        )
    return request


def encode_select_talent(value: SelectTalent) -> tuple[bytes, int]:
    """Serialize a client SelectTalent command for round-trip tests/tools."""

    if (
        not value.talent_id
        or not 1 <= int(value.rank_delta) <= MAX_TALENT_RANK_DELTA
    ):
        raise ValueError(
            "SelectTalent requires an id and a rank delta in "
            f"1..{MAX_TALENT_RANK_DELTA}"
        )
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(SELECT_TALENT_COMMAND.to_bytes(2, "little"))
    writer.write_string(value.talent_id)
    writer.write_uint32_le(int(value.rank_delta))
    return writer.finish()


def decode_deselect_talent(
    payload: bytes, bit_length: int | None = None
) -> DeselectTalent:
    """Decode client command ``0x0118`` (talent id): Honor focus talent
    right-click and switching an active trait off."""

    bit_length = len(payload) * 8 if bit_length is None else int(bit_length)
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x8B:
        raise ValueError("DeselectTalent does not begin with client marker 0x8b")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != DESELECT_TALENT_COMMAND:
        raise ValueError(
            f"expected DeselectTalent command 0x{DESELECT_TALENT_COMMAND:04x}, "
            f"got 0x{command_id:04x}"
        )
    request = DeselectTalent(reader.read_string())
    if not request.talent_id:
        raise ValueError("DeselectTalent requires a talent id")
    if reader.remaining:
        raise ValueError(
            f"DeselectTalent contains {reader.remaining} trailing bit(s)"
        )
    return request


def encode_deselect_talent(value: DeselectTalent) -> tuple[bytes, int]:
    if not value.talent_id:
        raise ValueError("DeselectTalent requires a talent id")
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(DESELECT_TALENT_COMMAND.to_bytes(2, "little"))
    writer.write_string(value.talent_id)
    return writer.finish()


def decode_unlock_skill_talent_preselection(
    payload: bytes, bit_length: int | None = None
) -> UnlockSkillTalentPreselection:
    """Decode client command ``0x011c`` (u32 bought Skill presets + 1)."""

    bit_length = len(payload) * 8 if bit_length is None else int(bit_length)
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x8B:
        raise ValueError(
            "UnlockSkillTalentPreselection does not begin with client "
            "marker 0x8b"
        )
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != UNLOCK_SKILL_TALENT_PRESELECTION_COMMAND:
        raise ValueError(
            "expected UnlockSkillTalentPreselection command "
            f"0x{UNLOCK_SKILL_TALENT_PRESELECTION_COMMAND:04x}, "
            f"got 0x{command_id:04x}"
        )
    request = UnlockSkillTalentPreselection(reader.read_uint32_le())
    if not 1 <= request.unlocked <= SKILL_TALENT_UNLOCKABLE_PRESELECTIONS:
        raise ValueError(
            f"UnlockSkillTalentPreselection count {request.unlocked} is "
            f"outside 1..{SKILL_TALENT_UNLOCKABLE_PRESELECTIONS}"
        )
    if reader.remaining:
        raise ValueError(
            "UnlockSkillTalentPreselection contains "
            f"{reader.remaining} trailing bit(s)"
        )
    return request


def encode_unlock_skill_talent_preselection(
    value: UnlockSkillTalentPreselection,
) -> tuple[bytes, int]:
    if not 1 <= int(value.unlocked) <= SKILL_TALENT_UNLOCKABLE_PRESELECTIONS:
        raise ValueError(
            "UnlockSkillTalentPreselection count is outside "
            f"1..{SKILL_TALENT_UNLOCKABLE_PRESELECTIONS}"
        )
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(
        UNLOCK_SKILL_TALENT_PRESELECTION_COMMAND.to_bytes(2, "little")
    )
    writer.write_uint32_le(int(value.unlocked))
    return writer.finish()


def _decode_two_byte_command(
    payload: bytes,
    *,
    command_id: int,
) -> tuple[int, int]:
    reader = BitReader(payload, len(payload) * 8)
    if reader.read_uint8() != 0x8B:
        raise ValueError("talent command does not begin with client marker 0x8b")
    actual = int.from_bytes(reader.read_bytes(2), "little")
    if actual != command_id:
        raise ValueError(
            f"expected talent command 0x{command_id:04x}, got 0x{actual:04x}"
        )
    values = (reader.read_uint8(), reader.read_uint8())
    if reader.remaining:
        raise ValueError(
            f"talent command 0x{command_id:04x} contains trailing bits"
        )
    return values


def decode_select_talent_preselection(
    payload: bytes,
) -> SelectTalentPreselection:
    """Decode retail ``0x0119`` as ``preselection, category``."""

    preselection, category = _decode_two_byte_command(
        payload, command_id=SELECT_TALENT_PRESELECTION_COMMAND
    )
    if not 0 <= category < len(TALENT_CATEGORY_PRESELECTION_COUNTS):
        raise ValueError("talent category is outside [0, 4)")
    if not 0 <= preselection < TALENT_CATEGORY_PRESELECTION_COUNTS[category]:
        raise ValueError("talent preselection is outside its category range")
    return SelectTalentPreselection(preselection, category)


def encode_select_talent_preselection(
    value: SelectTalentPreselection,
) -> tuple[bytes, int]:
    if not 0 <= value.category < len(TALENT_CATEGORY_PRESELECTION_COUNTS):
        raise ValueError("talent category is outside [0, 4)")
    if not (
        0
        <= value.preselection
        < TALENT_CATEGORY_PRESELECTION_COUNTS[value.category]
    ):
        raise ValueError("talent preselection is outside its category range")
    return (
        bytes(
            (
                0x8B,
                SELECT_TALENT_PRESELECTION_COMMAND & 0xFF,
                SELECT_TALENT_PRESELECTION_COMMAND >> 8,
                value.preselection,
                value.category,
            )
        ),
        40,
    )


def decode_reset_talent_preselection(
    payload: bytes,
) -> ResetTalentPreselection:
    """Decode retail ``0x011e`` as ``category, preselection``."""

    category, preselection = _decode_two_byte_command(
        payload, command_id=RESET_TALENT_PRESELECTION_COMMAND
    )
    if not 0 <= category < len(TALENT_CATEGORY_PRESELECTION_COUNTS):
        raise ValueError("talent category is outside [0, 4)")
    if not 0 <= preselection < TALENT_CATEGORY_PRESELECTION_COUNTS[category]:
        raise ValueError("talent preselection is outside its category range")
    return ResetTalentPreselection(category, preselection)


def encode_reset_talent_preselection(
    value: ResetTalentPreselection,
) -> tuple[bytes, int]:
    if not 0 <= value.category < len(TALENT_CATEGORY_PRESELECTION_COUNTS):
        raise ValueError("talent category is outside [0, 4)")
    if not (
        0
        <= value.preselection
        < TALENT_CATEGORY_PRESELECTION_COUNTS[value.category]
    ):
        raise ValueError("talent preselection is outside its category range")
    return (
        bytes(
            (
                0x8B,
                RESET_TALENT_PRESELECTION_COMMAND & 0xFF,
                RESET_TALENT_PRESELECTION_COMMAND >> 8,
                value.category,
                value.preselection,
            )
        ),
        40,
    )


def decode_reset_talent_category(
    payload: bytes,
) -> ResetTalentCategory:
    """Decode ``0x011f`` as category, branch resource id, preselection."""

    reader = BitReader(payload, len(payload) * 8)
    if reader.read_uint8() != 0x8B:
        raise ValueError(
            "ResetTalentCategory does not begin with client marker 0x8b"
        )
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != RESET_TALENT_CATEGORY_COMMAND:
        raise ValueError(
            f"expected ResetTalentCategory command "
            f"0x{RESET_TALENT_CATEGORY_COMMAND:04x}, "
            f"got 0x{command_id:04x}"
        )
    request = ResetTalentCategory(
        reader.read_uint8(),
        reader.read_string(),
        reader.read_uint8(),
    )
    if not 0 <= request.category < len(
        TALENT_CATEGORY_PRESELECTION_COUNTS
    ):
        raise ValueError("talent category is outside [0, 4)")
    if not (
        0
        <= request.preselection
        < TALENT_CATEGORY_PRESELECTION_COUNTS[request.category]
    ):
        raise ValueError("talent preselection is outside its category range")
    if not request.category_id:
        raise ValueError("ResetTalentCategory requires a branch resource id")
    if reader.remaining:
        raise ValueError(
            f"ResetTalentCategory contains {reader.remaining} trailing bit(s)"
        )
    return request


def encode_reset_talent_category(
    value: ResetTalentCategory,
) -> tuple[bytes, int]:
    if not 0 <= value.category < len(
        TALENT_CATEGORY_PRESELECTION_COUNTS
    ):
        raise ValueError("talent category is outside [0, 4)")
    if not (
        0
        <= value.preselection
        < TALENT_CATEGORY_PRESELECTION_COUNTS[value.category]
    ):
        raise ValueError("talent preselection is outside its category range")
    if not value.category_id:
        raise ValueError("ResetTalentCategory requires a branch resource id")
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(RESET_TALENT_CATEGORY_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(int(value.category))
    writer.write_string(str(value.category_id))
    writer.write_uint8(int(value.preselection))
    return writer.finish()


class TalentDatabase:
    """Read the local client's skill-talent definitions and manager indexes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        # Talent selection reads every selected definition more than once.
        # Reuse the thread's connection so each lookup does not reload SQLite's
        # schema on the UDP receive thread.
        return readonly_db.connect(self.path)

    def definition(self, talent_id: str) -> SkillTalentDefinition:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT [Id], [CharClass], [UnlockLevel], [MaxLevel], "
                "[UnlockCosts], [CostsPerLevel], [CostsPerLevelMultiplier], "
                "[SkillTemplateId], [BaseStatusEffectId], "
                "[StackStatusEffectId], [GroupId], [Category] "
                "FROM [_Template_SkillTalent] WHERE [Id] = ? LIMIT 1",
                (str(talent_id),),
            ).fetchone()
            if row is not None:
                definition = SkillTalentDefinition(
                    talent_id=str(row[0]),
                    class_name=str(row[1]),
                    unlock_level=max(1, int(row[2])),
                    max_rank=max(1, int(row[3])),
                    unlock_cost=max(0, int(row[4])),
                    cost_per_rank=max(0, int(row[5])),
                    cost_multiplier=float(row[6]),
                    skill_id=str(row[7] or ""),
                    base_status_effect_id=str(row[8] or ""),
                    stack_status_effect_id=str(row[9] or ""),
                    group_id=str(row[10] or ""),
                    selection_kind=str(row[11] or ""),
                    category_id=0,
                )
            else:
                generic = connection.execute(
                    "SELECT [Id], [CharClass], [UnlockLevel], [MaxLevel], "
                    "[UnlockCosts], [CostsPerLevel], "
                    "[CostsPerLevelMultiplier], [BaseStatusEffectId], "
                    "[StackStatusEffectId], [CategoryTemplate], "
                    "[TalentCategory] FROM [_Template_Talents] "
                    "WHERE [Id] = ? LIMIT 1",
                    (str(talent_id),),
                ).fetchone()
                if generic is not None:
                    category_id = self.category_id(str(generic[10]))
                    definition = SkillTalentDefinition(
                        talent_id=str(generic[0]),
                        class_name=str(generic[1] or ""),
                        unlock_level=max(0, int(generic[2])),
                        max_rank=max(1, int(generic[3])),
                        unlock_cost=max(0, int(generic[4])),
                        cost_per_rank=max(0, int(generic[5])),
                        cost_multiplier=float(generic[6]),
                        skill_id="",
                        base_status_effect_id=str(generic[7] or ""),
                        stack_status_effect_id=str(generic[8] or ""),
                        group_id=str(generic[9] or ""),
                        selection_kind="_Template_Talents",
                        category_id=category_id,
                    )
                else:
                    trait = connection.execute(
                        "SELECT [Id], [UnlockCosts], [StatusEffectId], "
                        "[UnlockLevel], [TalentCategory] "
                        "FROM [_Template_Trait] WHERE [Id] = ? LIMIT 1",
                        (str(talent_id),),
                    ).fetchone()
                    if trait is not None:
                        definition = SkillTalentDefinition(
                            talent_id=str(trait[0]),
                            class_name=self._class_from_talent_id(
                                str(trait[0])
                            ),
                            unlock_level=max(0, int(trait[3])),
                            max_rank=1,
                            unlock_cost=max(0, int(trait[1])),
                            cost_per_rank=max(0, int(trait[1])),
                            cost_multiplier=0.0,
                            skill_id="",
                            base_status_effect_id=str(trait[2] or ""),
                            stack_status_effect_id="",
                            group_id="",
                            selection_kind="_Template_Trait",
                            category_id=self.category_id(str(trait[4])),
                        )
                    else:
                        focus = connection.execute(
                            "SELECT f.[Id], f.[StatusEffectId], "
                            "f.[TalentCategory], s.[MaxStackSize] "
                            "FROM [_Template_FocusTalent] f "
                            "LEFT JOIN [_Template_StatusEffect] s "
                            "ON s.[Id] = f.[StatusEffectId] "
                            "WHERE f.[Id] = ? LIMIT 1",
                            (str(talent_id),),
                        ).fetchone()
                        if focus is None:
                            raise ValueError(
                                f"unknown talent {talent_id!r}"
                            )
                        definition = SkillTalentDefinition(
                            talent_id=str(focus[0]),
                            class_name="",
                            unlock_level=1,
                            max_rank=max(1, int(focus[3] or 1)),
                            unlock_cost=1,
                            cost_per_rank=1,
                            cost_multiplier=0.0,
                            skill_id="",
                            base_status_effect_id=str(focus[1] or ""),
                            stack_status_effect_id="",
                            group_id="",
                            selection_kind="_Template_FocusTalent",
                            category_id=self.category_id(str(focus[2])),
                        )
        if not definition.base_status_effect_id:
            raise ValueError(
                f"talent {talent_id!r} has no base status effect"
            )
        return definition

    @staticmethod
    def category_id(category_name: str) -> int:
        try:
            return TALENT_CATEGORY_NAMES.index(str(category_name))
        except ValueError as exc:
            raise ValueError(
                f"unknown talent category {category_name!r}"
            ) from exc

    @staticmethod
    def _class_from_talent_id(talent_id: str) -> str:
        # The client takes the first "_" token in either case.
        prefix = str(talent_id).split("_", 1)[0].casefold()
        return (
            prefix
            if prefix in {"dwarf", "mage", "ranger", "warrior", "niwalk"}
            else ""
        )

    def _global(self, name: str):
        if not re.fullmatch(r"[A-Za-z0-9_]+", str(name)):
            raise ValueError(f"invalid _Globals column {name!r}")
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT [{name}] FROM [_Globals] LIMIT 1"
            ).fetchone()
        if row is None:
            raise ValueError("_Globals has no row")
        return row[0]

    def unlock_preselection_costs(self) -> tuple[int, ...]:
        """Andermant price of each bought Skill preset, in order."""

        return tuple(
            int(part)
            for part in str(
                self._global("UnlockPreselectionCost") or ""
            ).split(";")
            if part.strip()
        )

    def talent_sets(self, membership_type: int, active: bool) -> int:
        """Presets of every tree the membership grants (NumTalentSets)."""

        privilege = (
            MEMBERSHIP_PRIVILEGES.get(int(membership_type))
            if active
            else None
        ) or "NoPrivileges"
        with self._connect() as connection:
            row = connection.execute(
                "SELECT [NumTalentSets] FROM [_Template_Privileges] "
                "WHERE [Id] = ? LIMIT 1",
                (privilege,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown privilege {privilege!r}")
        return max(1, int(row[0]))

    def respec_cost_per_point(self) -> int:
        return max(0, int(self._global("VCRespecCostsPerPoint") or 0))

    def respec_level_multiplier(self, level: int) -> float:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT [RespecVCCostMultiplier] "
                "FROM [_Template_DynamicLevelCosts] WHERE [Level] = ? LIMIT 1",
                (max(1, int(level)),),
            ).fetchone()
        if row is None:
            raise ValueError(f"no respec price for level {level}")
        return float(row[0])

    def preset_cooldown(
        self, category_id: int
    ) -> tuple[str, int, float] | None:
        """The preset cooldown effect of a tree: (id, wire id, seconds)."""

        if not 0 <= int(category_id) < len(TALENT_PRESET_COOLDOWN_EFFECTS):
            return None
        effect_id = TALENT_PRESET_COOLDOWN_EFFECTS[int(category_id)]
        with self._connect() as connection:
            row = connection.execute(
                "SELECT rowid, [StatusEffectDuration] "
                "FROM [_Template_StatusEffect] WHERE [Id] = ? LIMIT 1",
                (effect_id,),
            ).fetchone()
        if row is None:
            return None
        return effect_id, int(row[0]) - 1, max(0.0, float(row[1] or 0.0))

    def respec_cost_effect_wire_id(self) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT rowid FROM [_Template_StatusEffect] "
                "WHERE [Id] = ? LIMIT 1",
                (TALENT_RESPEC_COST_EFFECT,),
            ).fetchone()
        return None if row is None else int(row[0]) - 1

    def maximum_progress_xp(self, category_id: int) -> int:
        if category_id == 0:
            raise ValueError("skill progression uses the character level")
        category_name = TALENT_CATEGORY_NAMES[int(category_id)]
        with self._connect() as connection:
            value = connection.execute(
                "SELECT COALESCE(MAX([XP]), 0) "
                "FROM [_Template_TalentXPLevel] "
                "WHERE [TalentCategory] = ?",
                (category_name,),
            ).fetchone()[0]
        return max(0, int(value))

    def progression_level(self, category_id: int, experience: int) -> int:
        if category_id == 0:
            raise ValueError("skill progression uses the character level")
        category_name = TALENT_CATEGORY_NAMES[int(category_id)]
        with self._connect() as connection:
            value = connection.execute(
                "SELECT COALESCE(MAX([Level]), 0) "
                "FROM [_Template_TalentXPLevel] "
                "WHERE [TalentCategory] = ? AND [XP] <= ?",
                (category_name, max(0, int(experience))),
            ).fetchone()[0]
        return max(0, int(value))

    def granted_category_points(
        self, category_id: int, experience: int
    ) -> int:
        if category_id == 0:
            raise ValueError("use granted_skill_points for category zero")
        category_name = TALENT_CATEGORY_NAMES[int(category_id)]
        with self._connect() as connection:
            value = connection.execute(
                "SELECT COALESCE(SUM([TalentPointsGranted]), 0) "
                "FROM [_Template_TalentXPLevel] "
                "WHERE [TalentCategory] = ? AND [XP] <= ?",
                (category_name, max(0, int(experience))),
            ).fetchone()[0]
        return max(0, int(value))

    def category_group_max_rank(self, group_id: str) -> int:
        if not group_id:
            return 0
        with self._connect() as connection:
            row = connection.execute(
                "SELECT [MaxLevel] FROM [_Template_TalentCategories] "
                "WHERE [Id] = ? LIMIT 1",
                (str(group_id),),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown talent category group {group_id!r}")
        return max(0, int(row[0]))

    def granted_skill_points(self, level: int) -> int:
        with self._connect() as connection:
            value = connection.execute(
                "SELECT COALESCE(SUM([TalentPointsGranted]), 0) "
                "FROM [_Template_TalentXPLevelSkill] WHERE [Level] <= ?",
                (max(1, int(level)),),
            ).fetchone()[0]
        return max(0, int(value))

    def status_effect_wire_id(self, status_effect_id: str) -> int:
        """Return the zero-based StatusEffect manager index used on the wire."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT rowid FROM [_Template_StatusEffect] "
                "WHERE [Id] = ? LIMIT 1",
                (str(status_effect_id),),
            ).fetchone()
        if row is None:
            raise ValueError(
                f"unknown talent status effect {status_effect_id!r}"
            )
        return int(row[0]) - 1

    def status_effect_modifiers(self, status_effect_id: str) -> str:
        """Return the database modifier program applied by a talent effect.

        The client applies these modifiers when it receives the corresponding
        StatusEffect.  They include direct skill modifiers as well as nested
        ``SkillStatusEffect``/actor modifiers, so the server must keep every
        acquired rank effect active rather than treating the packet as a
        cosmetic talent-window acknowledgement.
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT [StartModifiers] FROM [_Template_StatusEffect] "
                "WHERE [Id] = ? LIMIT 1",
                (str(status_effect_id),),
            ).fetchone()
        if row is None:
            raise ValueError(
                f"unknown talent status effect {status_effect_id!r}"
            )
        return str(row[0] or "")

    def modifiers_for_rank(
        self,
        definition: SkillTalentDefinition,
        target_rank: int,
    ) -> tuple[SkillTalentModifier, ...]:
        """Parse the modifier program installed by one acquired rank.

        The server sends the StatusEffect manager index, and the client then
        executes this database-owned program. Keeping this parser alongside
        selection validation lets tests verify that damage, victim effects,
        and user effects are all retained instead of acknowledging only the
        talent-window rank.
        """

        instructions = []
        for status_effect_id in self.status_effect_ids_for_rank(
            definition, target_rank
        ):
            for raw_instruction in self.status_effect_modifiers(
                status_effect_id
            ).split(";"):
                raw_instruction = raw_instruction.strip()
                if not raw_instruction:
                    continue
                kind, separator, raw_arguments = raw_instruction.partition(":")
                if not separator or not kind.strip():
                    raise ValueError(
                        f"talent status effect {status_effect_id!r} contains "
                        f"an invalid modifier {raw_instruction!r}"
                    )
                instructions.append(
                    SkillTalentModifier(
                        kind.strip(),
                        tuple(
                            argument.strip()
                            for argument in raw_arguments.split(",")
                        ),
                    )
                )
        return tuple(instructions)

    @staticmethod
    def status_effect_ids_for_rank(
        definition: SkillTalentDefinition,
        target_rank: int,
    ) -> tuple[str, ...]:
        """Return the effects which jointly represent one selected rank.

        Talents with a distinct stack effect keep the base unlock effect and
        add one stack effect from rank 2 onward.  Ordinary spell ranks use one
        stackable base effect.
        """

        if target_rank <= 0 or target_rank > definition.max_rank:
            raise ValueError(
                f"talent rank {target_rank} is outside "
                f"1..{definition.max_rank}"
            )
        if (
            target_rank > 1
            and definition.stack_status_effect_id
            and definition.stack_status_effect_id
            != definition.base_status_effect_id
        ):
            return (
                definition.base_status_effect_id,
                definition.stack_status_effect_id,
            )
        return (definition.base_status_effect_id,)

    def status_effect_entry(
        self,
        definition: SkillTalentDefinition,
        *,
        target_rank: int,
        actor_id: int,
        instance_id: int,
        server_tick: int,
    ) -> StatusEffectEntry:
        """Build the primary add-effect shape observed beside SelectTalent."""

        return self.status_effect_entries(
            definition,
            target_rank=target_rank,
            actor_id=actor_id,
            first_instance_id=instance_id,
            server_tick=server_tick,
        )[0]

    def status_effect_entries(
        self,
        definition: SkillTalentDefinition,
        *,
        target_rank: int,
        actor_id: int,
        first_instance_id: int,
        server_tick: int,
    ) -> tuple[StatusEffectEntry, ...]:
        """Build the complete editable effect set for one selected talent."""

        effect_ids = self.status_effect_ids_for_rank(
            definition, target_rank
        )
        distinct_stack = len(effect_ids) == 2
        entries = []
        for offset, status_effect_id in enumerate(effect_ids):
            if distinct_stack:
                stack_value = 0 if offset == 0 else target_rank - 2
            else:
                stack_value = target_rank - 1
            entries.append(StatusEffectEntry(
            effect_id=self.status_effect_wire_id(status_effect_id),
            field_04=int(first_instance_id) + offset,
            field_0c=0,
            field_14=25,
            field_08=int(server_tick) & 0xFFFFFFFF,
            field_18=stack_value,
            field_10=25,
            field_1c=100,
            field_20=int(actor_id),
            field_25=False,
            field_26=stack_value > 0,
            field_39=True,
            has_details=True,
            values=(0.0, 0.0, 0.0, 0.0, 0.0),
            field_38=False,
            field_3c=0xFF,
            shape_values=(),
            ))
        return tuple(entries)
