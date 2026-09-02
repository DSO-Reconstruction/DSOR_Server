"""What an item is, and what wearing it does.

This is the table this project kept arriving at from three directions at once. The
operator's three open complaints -- the `On:` triggers that cannot be raised, the stats
that never change, the inventory that does not persist -- all wait on the same missing
thing: a model of what a character owns. It turns out the client ships it whole, in two
tables.

``_Template_Item`` says what an item *is*. The torso the operator picked up during the
``officiel4`` capture reads:

    SlotType          TorsoSlot
    CharClass         warrior
    ItemCategory      Armor
    BaseEnchantments  item_base_armor_torso;item_base_block_torso;
                      item_base_speed_movement_torso
    MinDropLevel 1    MaxDropLevel 200    RequiredLevel 1

``_Template_Enchantment`` says what a statistic *does*, and its ``Modifiers`` column is
the whole vocabulary of what a character's numbers are made of:

    <Attribute>:<value>[|<max>],<absolute|relative>[,<qualifier>...]

Settled across all 4,108 rows: 2,562 carry attribute and mode, 1,270 add one qualifier,
270 add five (the damage types), 6 add two. And there are only **24 attributes in the
whole game** -- ItemDamage (969 uses), ItemResistance (755), ItemArmor (494),
MaxHealthPoints (349), ItemCritical (292), ItemSpeed (290), ItemCriticalValue (235),
ItemHealthPoints (178), ItemBlockValue (158), Resistance (155), ItemRiftForce (100),
MaxSkillResource (34), Block (22), XPGain (14), Speed (13), Critical (12), DropAmount
(10), SkillResourceRegeneration (6), HealthPointsRegeneration (5), CriticalValue (5) and
four rarer. That is the answer to "how do the stats work": a character's stats are the
sum of their items' enchantments, in those 24 names.

**What the wire carries, and what it does not.** An item record in an
``InventoryInfoCommand`` names its rolled enchantments and gives each a value between 0
and 1 -- the roll, not the result. The torso carried ``item_block_torso`` at 0.8079 and
``item_armor_torso`` at 0.8210. The base enchantments its template names are **not** in
the record at all; they are implied by the template. So reading an item means both
tables, and the record alone is not enough.

**Where this stops, deliberately.** A modifier that states a range interpolates:
``ItemArmor:0.599|1.498,absolute`` at a roll of 0.821 is 1.337 per level of the item, and
the base torso enchantments all read that way. A modifier that states one value --
``ItemArmor:0.204,relative`` -- does not say what the roll means, and there are two
readings: the roll scales it, or the roll is irrelevant and the value stands. This module
returns the range case resolved and the single case stated, rather than choosing. The
same goes for the record's own ``third`` and ``fourth`` fields, which are 5 and 125 on
that torso: 125 looks like the item's level and 5 like a tier, and neither is proven, so
neither is used to scale anything here.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from dsor import database

#: Where an item can be worn. The 22 values ``_Template_Item.SlotType`` takes, in
#: descending order of how many items name them.
SLOTS: tuple[str, ...] = (
    "ConsumableSlot",
    "GlovesSlot",
    "HelmetSlot",
    "ShoulderSlot",
    "TorsoSlot",
    "BootsSlot",
    "CloakSlot",
    "LeftHandSlot",
    "RightHandSlot",
    "AmuletSlot",
    "RingSlot",
    "WeaponModSlot",
    "EmblemSlot",
    "AmmoSlot",
    "BeltSlot",
    "MercenaryAmuletSlot",
    "MercenaryBeltSlot",
    "MercenaryCloakSlot",
    "AncientTrinketSlot",
    "RuneTrinketSlot",
    "JewelTrinketSlot",
)

#: The slots a character wears, which is what the live service's inventory command maps
#: fourteen items into. The count matches; the *order* does not follow from anything read
#: so far, so this names them without claiming an index for each.
WORN = (
    "HelmetSlot",
    "TorsoSlot",
    "GlovesSlot",
    "BootsSlot",
    "ShoulderSlot",
    "BeltSlot",
    "CloakSlot",
    "AmuletSlot",
    "RingSlot",
    "RightHandSlot",
    "LeftHandSlot",
    "EmblemSlot",
    "AmmoSlot",
    "WeaponModSlot",
)

#: Rarities, worst to best. From ``MinItemRarity``/``MaxItemRarity`` and
#: ``DefaultRarity``; ``Set`` appears only as a default and sits outside the ladder.
RARITIES: tuple[str, ...] = (
    "Common",
    "Uncommon",
    "Magic",
    "Rare",
    "Epic",
    "Unique",
    "Mythic",
)

#: How a modifier applies. ``NoScaling`` is a ``ScalingMode``, not a modifier mode.
ABSOLUTE = "absolute"
RELATIVE = "relative"

ITEMS = "_Template_Item"
ENCHANTMENTS = "_Template_Enchantment"

#: What separates the entries in ``BaseEnchantments`` and ``SlotType``.
LIST = ";"
SLOT_LIST = ","


def _number(text) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Modifier:
    """One ``Attribute:value[|max],mode[,qualifier...]`` from a ``Modifiers`` column."""

    attribute: str
    low: float
    high: float | None
    mode: str
    qualifiers: tuple[str, ...] = ()

    @property
    def ranged(self) -> bool:
        """Whether it states two ends, which is what makes a roll mean something."""
        return self.high is not None

    @property
    def condition(self) -> str:
        """What an ``&`` in the mode names, or an empty string.

        Eight rows out of 4,108 write ``relative&emperorgold`` and
        ``relative&rarityunique``, so the mode field can carry a condition. Rare enough
        to have been missed by a spot check and common enough to break a parser that
        compares the whole field to "relative".
        """
        _, _, rest = self.mode.partition("&")
        return rest

    @property
    def relative(self) -> bool:
        return self.mode.startswith(RELATIVE)

    @property
    def absolute(self) -> bool:
        return self.mode.startswith(ABSOLUTE)

    def at(self, roll: float) -> float:
        """The value at *roll*, for a modifier that states a range.

        Refuses rather than guesses for a single-valued modifier: see the module
        docstring for the two readings, neither of which is established.
        """
        if not self.ranged:
            raise ValueError(
                f"{self.attribute} states one value ({self.low}); "
                "what a roll does to it is not established"
            )
        kept = 0.0 if roll < 0.0 else 1.0 if roll > 1.0 else roll
        return self.low + kept * (self.high - self.low)


def parse_modifiers(text: str | None) -> tuple[Modifier, ...]:
    """Every modifier in one ``Modifiers`` column.

    A malformed entry is skipped rather than guessed at, which is the same rule
    :func:`dsor.effects.parse_entries` follows for the skill columns.
    """
    if not text:
        return ()
    found: list[Modifier] = []
    for chunk in text.split(LIST):
        chunk = chunk.strip()
        if not chunk:
            continue
        fields = [field.strip() for field in chunk.split(SLOT_LIST)]
        head = fields[0]
        attribute, _, value = head.partition(":")
        attribute = attribute.strip()
        if not attribute or len(fields) < 2:
            continue
        low_text, _, high_text = value.partition("|")
        low = _number(low_text)
        if low is None:
            continue
        found.append(
            Modifier(
                attribute=attribute,
                low=low,
                high=_number(high_text) if high_text else None,
                mode=fields[1].lower(),
                qualifiers=tuple(fields[2:]),
            )
        )
    return tuple(found)


@dataclass(frozen=True)
class Enchantment:
    """One row of ``_Template_Enchantment``: a statistic an item can carry."""

    id: str
    slots: tuple[str, ...]
    character_class: str
    category: str
    modifiers: tuple[Modifier, ...]
    base: bool
    lowest_level: int
    highest_level: int
    lowest_rarity: str
    highest_rarity: str
    affinity: str
    scaling: str

    def fits(self, piece: "Piece", level: int | None = None) -> bool:
        """Whether *piece* could carry it.

        Class, category, slot and the item-level window, all four of which the row
        states. An empty class or category in the row means "any", which is how the
        general enchantments are written.
        """
        if self.character_class and piece.character_class:
            if self.character_class != piece.character_class:
                return False
        if self.category and piece.category and self.category != piece.category:
            return False
        if self.slots and piece.slot and piece.slot not in self.slots:
            return False
        if level is not None and not self.lowest_level <= level <= self.highest_level:
            return False
        return True


@dataclass(frozen=True)
class Piece:
    """One row of ``_Template_Item``: what an item is before it is rolled."""

    template: str
    slot: str
    category: str
    character_class: str
    base_enchantments: tuple[str, ...]
    lowest_drop: int
    highest_drop: int
    required_level: int
    rarity: str
    icon: str
    skin: str
    title: str

    @property
    def wearable(self) -> bool:
        return self.slot in WORN

    def droppable_at(self, level: int) -> bool:
        return self.lowest_drop <= level <= self.highest_drop


@cache
def enchantments() -> dict[str, Enchantment]:
    """Every statistic an item can carry, by name."""
    found: dict[str, Enchantment] = {}
    for row in database.rows(
        ENCHANTMENTS,
        "Id",
        "SlotType",
        "CharClass",
        "ItemCategory",
        "Modifiers",
        "BaseEnchantment",
        "MinItemLevel",
        "MaxItemLevel",
        "MinItemRarity",
        "MaxItemRarity",
        "RequiresEnchantmentAffinity",
        "ScalingMode",
    ):
        (
            _wire, name, slots, character_class, category, modifiers, base,
            lowest, highest, lowest_rarity, highest_rarity, affinity, scaling,
        ) = row
        if not name:
            continue
        found[name] = Enchantment(
            id=name,
            slots=tuple(s.strip() for s in (slots or "").split(SLOT_LIST) if s.strip()),
            character_class=(character_class or "").strip(),
            category=(category or "").strip(),
            modifiers=parse_modifiers(modifiers),
            base=bool(base),
            lowest_level=int(lowest or 1),
            highest_level=int(highest or 200),
            lowest_rarity=(lowest_rarity or "").strip(),
            highest_rarity=(highest_rarity or "").strip(),
            affinity=(affinity or "").strip(),
            scaling=(scaling or "").strip(),
        )
    return found


def enchantment(name: str) -> Enchantment | None:
    return enchantments().get(name)


@cache
def pieces() -> dict[str, Piece]:
    """Every item, by template name."""
    found: dict[str, Piece] = {}
    for row in database.rows(
        ITEMS,
        "Id",
        "SlotType",
        "ItemCategory",
        "CharClass",
        "BaseEnchantments",
        "MinDropLevel",
        "MaxDropLevel",
        "RequiredLevel",
        "DefaultRarity",
        "IconBrush",
        "EquipmentSkinId",
        "LocaleIdTitle",
    ):
        (
            _wire, template, slot, category, character_class, base,
            lowest, highest, required, rarity, icon, skin, title,
        ) = row
        if not template:
            continue
        found[template] = Piece(
            template=template,
            slot=(slot or "").strip(),
            category=(category or "").strip(),
            character_class=(character_class or "").strip(),
            base_enchantments=tuple(
                name.strip() for name in (base or "").split(LIST) if name.strip()
            ),
            lowest_drop=int(lowest or 0),
            highest_drop=int(highest or 0),
            required_level=int(required or 1),
            rarity=(rarity or "").strip(),
            icon=(icon or "").strip(),
            skin=(skin or "").strip(),
            title=(title or "").strip(),
        )
    return found


def piece(template: str) -> Piece | None:
    return pieces().get(template)


def slot_of(template: str) -> str | None:
    found = piece(template)
    return found.slot if found is not None else None


def rolled_for(template: str, level: int | None = None) -> list[Enchantment]:
    """The statistics an item of *template* could roll, base ones excluded.

    The base ones are excluded because they are not what a record carries: the torso in
    the capture named ``item_armor_torso`` and ``item_block_torso``, while its template
    names ``item_base_armor_torso`` and two others. Two lists, and only one travels.
    """
    found = piece(template)
    if found is None:
        return []
    return [
        candidate
        for candidate in enchantments().values()
        if not candidate.base and candidate.fits(found, level)
    ]


def droppable(
    level: int, character_class: str | None = None, slot: str | None = None
) -> list[Piece]:
    """The items that can drop at *level*, wearable ones only.

    Class-neutral items are kept for every class: the column is empty for them, and
    that is how jewellery and consumables are written.
    """
    out = []
    for found in pieces().values():
        if not found.wearable or not found.droppable_at(level):
            continue
        if slot is not None and found.slot != slot:
            continue
        if (
            character_class
            and found.character_class
            and found.character_class != character_class
        ):
            continue
        out.append(found)
    return out


def attributes_of(
    template: str, statistics: dict[str, float] | None = None
) -> dict[str, float]:
    """What one item contributes, by attribute name.

    Both halves: the base enchantments its template names, and the rolled ones the wire
    record carries in *statistics* as ``name -> roll``. A modifier stating a range is
    resolved at its roll -- or at its midpoint, for a base enchantment, whose roll the
    record does not carry. A modifier stating one value is added as it stands, because
    what a roll does to it is not established.

    Per level of the item, not absolute: the base torso enchantment reads
    ``ItemArmor:0.599|1.498,absolute`` and a level 125 torso is worth 125 times that. The
    scaling is deliberately left to the caller, which knows the item's level and can say
    where it got it.
    """
    out: dict[str, float] = {}
    found = piece(template)
    if found is None:
        return out

    def add(modifier: Modifier, roll: float | None) -> None:
        if modifier.ranged:
            value = modifier.at(0.5 if roll is None else roll)
        else:
            value = modifier.low
        out[modifier.attribute] = out.get(modifier.attribute, 0.0) + value

    for name in found.base_enchantments:
        carried = enchantment(name)
        if carried is None:
            continue
        for modifier in carried.modifiers:
            add(modifier, None)
    for name, roll in (statistics or {}).items():
        carried = enchantment(name)
        if carried is None:
            continue
        for modifier in carried.modifiers:
            add(modifier, roll)
    return out


def loaded() -> bool:
    """Whether both tables were there to read."""
    return bool(pieces()) and bool(enchantments())
