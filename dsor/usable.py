"""Using an item, which is how a mount is summoned.

The operator rode a manticore during the ``officiel4`` capture, and there is no mount
command: there is an item, used by name, answered by two status effects.

    client   0x0135   1b 00 "mythical_mount_manticore_01"
    server   0x004F   cast_mount_manticore_01, 87 ticks
    server   0x004F   ride_mount_manticore_01, 900,000 ticks

The rest is a column. ``_Template_Item.StatusEffectId`` names the effect the item
applies -- 2,862 of the client's 15,645 items carry one, and 265 of those are mounts --
so nothing has to be mangled out of the item's name. The manticore's row says
``cast_mount_manticore_01`` with a ``StatusEffectDuration`` of **3.5** seconds, and the
live service put 87 ticks on the wire: 87 / 25 = 3.48. So the duration comes from the
item and not from the effect table, which says 6.0 for the same effect.

The second effect is the ride itself, and its name is the first with ``cast`` swapped for
``ride``. 900,000 ticks is 36,000 seconds, which is exactly the effect table's duration
for it: ten hours, meaning "until you get off".
"""

from __future__ import annotations

from dataclasses import dataclass

from dsor import database, effects

#: The command the client sends. Its class in the client's Rtti is
#: ``UseStickerBookItemCommand``, which is not what it does here: the body is a
#: length-prefixed item template name and nothing else.
USE_ITEM = 0x0135

#: The table that says what an item does when used.
TABLE = "_Template_Item"

#: How a summon names its ride.
SUMMON = "cast_"
RIDE = "ride_"

_loaded: dict[str, "Usable"] | None = None


@dataclass(frozen=True)
class Usable:
    """An item that applies a status effect when used."""

    template: str
    effect: str
    duration: float
    values: tuple[float, float, float, float, float]

    @property
    def is_mount(self) -> bool:
        return self.effect.startswith(SUMMON + "mount_")

    @property
    def ride(self) -> str | None:
        """The effect that keeps the player on the mount, if this is one."""
        if not self.is_mount:
            return None
        wanted = RIDE + self.effect[len(SUMMON) :]
        return wanted if effects.by_id(wanted) is not None else None


def loaded() -> dict[str, Usable]:
    """Every item that names a status effect, by template name."""
    global _loaded
    if _loaded is not None:
        return _loaded
    found: dict[str, Usable] = {}
    for row in database.rows(
        TABLE,
        "Id",
        "StatusEffectId",
        "StatusEffectDuration",
        "StatusEffectValue0",
        "StatusEffectValue1",
        "StatusEffectValue2",
        "StatusEffectValue3",
        "StatusEffectValue4",
    ):
        _wire, template, effect, duration, *values = row
        if not effect:
            continue
        found[template] = Usable(
            template=template,
            effect=effect,
            duration=float(duration or 0.0),
            values=tuple(float(value or 0.0) for value in values),  # type: ignore[arg-type]
        )
    _loaded = found
    return found


def by_template(template: str) -> Usable | None:
    return loaded().get(template)


def entries_for(template: str) -> list[effects.Entry]:
    """What using *template* applies, in the order the live service sent it.

    Empty for an item that names no effect, or one whose effect the client's own table
    does not have -- rather than a guess, because an effect index the client cannot
    resolve creates nothing and logs nothing.
    """
    item = by_template(template)
    if item is None:
        return []
    out: list[effects.Entry] = []
    found = effects.by_id(item.effect)
    if found is not None:
        out.append(
            effects.Entry(
                item.effect, effects.CERTAIN, item.duration or None, None, item.values
            )
        )
    ride = item.ride
    if ride is not None:
        riding = effects.by_id(ride)
        out.append(
            effects.Entry(ride, effects.CERTAIN, riding.duration, None, item.values)
        )
    return out


def name_in(body: bytes) -> str | None:
    """The item template name a 0x0135 carries: a 16-bit length and that many bytes."""
    if len(body) < 2:
        return None
    length = int.from_bytes(body[:2], "little")
    if length == 0 or len(body) < 2 + length:
        return None
    try:
        return body[2 : 2 + length].decode("ascii")
    except UnicodeDecodeError:
        return None
