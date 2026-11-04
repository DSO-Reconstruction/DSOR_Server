"""The Night Walker's first spells, authored over the original's placeholders.

The original left 26 niwalk skills in _Template_Skill (CharClass niwalk) with
their names, types and rough ranges, but dummy sequences, no costs, no effects
and IsLive=0. Each spell here takes a live skill of the same SkillType as its
"donor" -- its geometry, frames, bullet, sequence mappings and damage type, so
the client plays and the server resolves it as that skill does -- and then
its own values: blood gained or spent (ResourceGain / ResourceCost, fractions
of the pool, as the warrior's rage), cooldown, damage, unlock level and the
status effects it lays.

tools/install_niwalk_content.py writes these into a static.db4 in place (the
rows keep their rowids: a skill's wire index is rowid - 1). Server and client
read the same file, then the client's converters export the skills again.

Blood (ResourceNiwalk) behaves as rage: the quick cuts (zero_) fill it, every
other spell spends it, and the sacrifice_ spells also cost a part of the Night
Walker's own life (talent_bloodmage_self_damage, $0 relative to its health).
"""
from __future__ import annotations

# A skill's own columns, never taken from its donor.
OWN_COLUMNS = frozenset((
    "Id", "Name", "CharClass", "IconBrush", "Groups", "CoolDownCategory",
    "CoolDown", "InitialCoolDown", "UnlockLevel", "IsLive", "Hidden",
    "ResourceCost", "ResourceGain", "ResourceCostToDamage", "AmmoCost",
    "DamageModifier", "VictimStatusEffects", "UserStatusEffects",
    "LocationStatusEffects", "MinDifficulty", "MaxDifficulty", "SummonCategory",
))

BLEEDING = "debuff_dot_bleeding,C:1.0,D:5.0,DP:5.0"
STUN_SHORT = "debuff_cc_stun,C:1.0,D:1.5,DP:1.0"
FROST = "debuff_cc_frost,C:1.0,D:3.0,DP:2.0"
POISON = "debuff_dot_poison,C:1.0,D:2.0,DP:2.0"
# The caster pays a share of its own health (relative, no crit, no incidents).
def SELF_DAMAGE(share: float) -> str:
    return f"talent_bloodmage_self_damage,C:1.0,D:0.0,DP:0.0,$0:-{share:g}"
# Healed for a share of the damage it deals while the leech lasts.
def LIFE_LEECH(share: float, seconds: float) -> str:
    return f"skill_frenzyshout_buff_lifeleech,C:1.0,D:{seconds:g},DP:{seconds:g},$0:{share:g}"

NIWALK_SKILLS: dict[str, dict] = {
    # Slice & Dice: the left button, two quick cuts; fills blood.
    "zero_slicedice": dict(
        donor="angrystrike", unlock=1, damage=1.0, gain=0.06, cost=0.0, cooldown=0.0,
        victim=(), user=(),
    ),
    # Blade Cut (offensive): a deep cut that bleeds; fills blood.
    # UNVERIFIED: zero_bladecut_def (crippling) is the talent's other half.
    "zero_bladecut_off": dict(
        donor="mightystab", unlock=2, damage=1.4, gain=0.05, cost=0.0, cooldown=3.0,
        victim=(BLEEDING,), user=(),
    ),
    # Shoulder Dash: a 10 m charge that knocks down what it meets.
    "move_shoulderdash": dict(
        donor="stuncharge", unlock=4, damage=1.0, gain=0.0, cost=0.25, cooldown=6.0,
        victim=(STUN_SHORT,), user=(),
    ),
    # Flashbang Escape: a jump away in a flash.
    "move_flashbang": dict(
        donor="teleport", unlock=6, damage=0.0, gain=0.0, cost=0.2, cooldown=8.0,
        victim=(), user=(),
    ),
    # Blood Cut: a heavy blow paid with 5 % of the Night Walker's life, which
    # drinks back a quarter of the damage it deals for 4 s.
    "sacrifice_cut": dict(
        donor="mightystab", unlock=8, damage=3.0, gain=0.0, cost=0.3, cooldown=6.0,
        victim=(BLEEDING,), user=(SELF_DAMAGE(0.05), LIFE_LEECH(0.25, 4.0)),
    ),
    # Gas Bomb: a thrown flask whose cloud poisons and slows for 6 s.
    "trickery_gasbomb": dict(
        donor="Grenade", unlock=10, damage=1.2, gain=0.0, cost=0.35, cooldown=10.0,
        victim=(POISON,), user=(),
        location=("poisonous_puddle_aura,C:1.0,D:6.0,DP:6.0,TR:1.0",),
    ),
    # Blood Vial: a vial that falls 12 m away, paid with 8 % of life; what it
    # hits bleeds.
    "sacrifice_vial": dict(
        donor="stunshot", unlock=12, damage=2.2, gain=0.0, cost=0.3, cooldown=10.0,
        victim=(BLEEDING,), user=(SELF_DAMAGE(0.08),),
    ),
    # Glass Dagger: a fan of daggers thrown ahead; they chill what they cut.
    "crowd_glassdagger": dict(
        donor="multishot", unlock=14, damage=0.8, gain=0.0, cost=0.3, cooldown=4.0,
        victim=(FROST,), user=(),
    ),
    # Windmill Slash (offensive): the ultimate, a whirl of both blades that
    # leaves every enemy around bleeding.
    # UNVERIFIED: ultimate_windmill_def (the cloud) is the talent's other half.
    "ultimate_windmill_off": dict(
        donor="bloody360", unlock=20, damage=1.6, gain=0.0, cost=0.6, cooldown=20.0,
        victim=(BLEEDING,), user=(),
    ),
}

# The Night Walker's starting quick bar (left button, right button, keys 1-5):
# what is unlocked at level 1, as the other classes start with their first.
NIWALK_START_QUICKBAR = ("zero_slicedice",)


def joined(effects) -> str:
    return "".join(f"{effect};" for effect in effects)


def authored_row(skill_id: str, donor: dict, current: dict) -> dict:
    """The columns tools/install_niwalk_content.py writes for one skill."""

    spec = NIWALK_SKILLS[skill_id]
    row = {column: value for column, value in donor.items() if column not in OWN_COLUMNS}
    row.update(
        Groups=f"niwalk_skills;{skill_id};player_skills;",
        CoolDownCategory=f"niwalk_{skill_id}",
        CoolDown=float(spec["cooldown"]),
        UnlockLevel=int(spec["unlock"]),
        IsLive=1,
        Hidden=0,
        ResourceCost=float(spec["cost"]),
        ResourceGain=float(spec["gain"]),
        ResourceCostToDamage=0.0,
        AmmoCost=0,
        DamageModifier=float(spec["damage"]),
        VictimStatusEffects=joined(spec["victim"]),
        UserStatusEffects=joined(spec["user"]),
        LocationStatusEffects=joined(spec.get("location", ())),
        MinDifficulty=donor.get("MinDifficulty", 0),
        MaxDifficulty=donor.get("MaxDifficulty", 10),
    )
    # The icon and the name stay the Night Walker's own.
    row.pop("Name", None)
    return {column: value for column, value in row.items() if current.get(column) != value}
