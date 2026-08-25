"""Every actor attribute the protocol can name.

Recovered from ``Attribute::ActorAttributeIdToString``, which the client compiles
to a jump table: ``cmp eax, 0x53`` over the value plus one, then 84 four-byte
offsets at RVA 0x800dfc, each landing on a block that loads its own name. So the
list is complete and in order by construction — not a guess assembled from strings
found lying around, which is how the same binary produced a plausible and wrong
skill list earlier.

The identifiers matter because an actor's stats travel as a dictionary of these to
floats: ``Util::Dictionary<Game::Attribute::ActorAttributeId, float>``, written
through ``IO::Adapter<Network::BitWriter, ActorAttributeId>``.

Note that ``ActorStatsUpdateCommand`` 0x007B is *not* that dictionary: it carries
current hit points as an int64 and the skill resource as a float32, which is
confirmed on the wire. The dictionary lives with the actor itself.
"""

from __future__ import annotations

#: Attribute id to name, exactly as the client enumerates them.
ATTRIBUTES: dict[int, str] = {
      -1: 'InvalidActorAttribute',
       0: 'MovementSpeed',
       1: 'SkillExecutionSpeed',
       2: 'MaxHealthPoints',
       3: 'MaxHealthPointsForMonsterLeader',
       4: 'HealthPointsRegeneration',
       5: 'AbsoluteHealthPointsRegeneration',
       6: 'MaxSkillResource',
       7: 'SkillResourceRegeneration',
       8: 'AbsoluteSkillResourceRegeneration',
       9: 'MaxShield',
      10: 'ShieldRegenerationDelay',
      11: 'MinDamage',
      12: 'MaxDamage',
      13: 'DPSBonus',
      14: 'DamageFactorPvP',
      15: 'DamageFactorPvE',
      16: 'DamagePermeabilityPvP',
      17: 'DamageReflection',
      18: 'SupportScorePvP',
      19: 'Armor',
      20: 'AllResistance',
      21: 'FireResistance',
      22: 'IceResistance',
      23: 'LightningResistance',
      24: 'DarkMagicResistance',
      25: 'PoisonResistance',
      26: 'Block',
      27: 'Critical',
      28: 'XPGain',
      29: 'GroupXPGain',
      30: 'TwinksHonorGain',
      31: 'HonorGain',
      32: 'GuildHonorGain',
      33: 'EventHonorGain',
      34: 'LargeWeaponDamageFactor',
      35: 'SmallWeaponDamageFactor',
      36: 'LargeWeaponDamageBonus',
      37: 'SmallWeaponDamageBonus',
      38: 'SkillResourceGainOnDamageReceived',
      39: 'SkillResourceGainOnDamageDealt',
      40: 'LifeLeechBase',
      41: 'HealthGlobeHitPoints',
      42: 'HealthGlobeSkillResource',
      43: 'VCBuyPriceFactor',
      44: 'VCSellPriceFactor',
      45: 'DropChanceFactor',
      46: 'QuestExperience',
      47: 'AnimRunSpeed',
      48: 'RepeatableQuestDropFactor',
      49: 'HeroicRepeatableQuestDropFactor',
      50: 'EventRepeatableQuestDropFactor',
      51: 'RowRepeatableQuestDropFactor',
      52: 'LocalQuestDropFactor',
      53: 'WorldQuestDropFactor',
      54: 'HeroicQuestDropFactor',
      55: 'EventQuestDropFactor',
      56: 'PvPQuestDropFactor',
      57: 'AllQuestDropFactor',
      58: 'MovementBonusSpeed',
      59: 'LargeWeaponSpeedFactor',
      60: 'SmallWeaponSpeedFactor',
      61: 'LargeWeaponSpeedBonus',
      62: 'SmallWeaponSpeedBonus',
      63: 'TalentRespecCostFactor',
      64: 'CraftingQuestDropFactor',
      65: 'AdditionalSkillCost',
      66: 'IgnoreSkillCost',
      67: 'RiftForce',
      68: 'RiftEchoChance',
      69: 'RiftEcho',
      70: 'TargetPhysicalReduceResistance',
      71: 'TargetFireReduceResistance',
      72: 'TargetIceReduceResistance',
      73: 'TargetLightningReduceResistance',
      74: 'TargetDarkMagicReduceResistance',
      75: 'TargetPoisonReduceResistance',
      76: 'TargetPhysicalAddDamage',
      77: 'TargetFireAddDamage',
      78: 'TargetIceAddDamage',
      79: 'TargetLightningAddDamage',
      80: 'TargetDarkMagicAddDamage',
      81: 'TargetPoisonAddDamage',
      82: 'FinalDamageIncrease',
}

#: Name to id, for looking one up the other way round.
BY_NAME: dict[str, int] = {name: value for value, name in ATTRIBUTES.items()}

INVALID = -1


def attribute_id(name: str) -> int:
    """The id *name* has, or :data:`INVALID`."""
    return BY_NAME.get(name, INVALID)


def attribute_name(value: int) -> str:
    """The name *value* has, or the invalid one."""
    return ATTRIBUTES.get(value, ATTRIBUTES[INVALID])
