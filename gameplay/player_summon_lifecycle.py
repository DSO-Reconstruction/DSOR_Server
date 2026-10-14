"""Generated lifecycle packets for player-owned monster constructions.

Retail prepares a turret as a health/faction/status actor before announcing it
through vicinity and ``NewMonster``.  Keeping these builders separate from the
combat simulation makes the ordering explicit and keeps protocol-only details
out of the authoritative damage loop.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import sqlite3

from dso_server.protocol.gameplay_protocol import (
    FactionInfo,
    MonsterSpawnBatch,
    MonsterUpdate,
    MonsterUpdateBatch,
    NewMonsterServerCommand,
    StatusEffect,
    StatusEffectEntry,
    StatusEffectServerCommand,
)
from dso_server.gameplay.local_monster_state import MonsterSpawnGroup


SERVER_TICKS_PER_SECOND = 25
SUMMON_AURA_REFRESH_TICKS = 12
# Renew halfway through the lease. Retail can publish this generated aura on
# every game tick, but doing so over this reconstructed reliable transport
# creates a self-sustaining retry backlog after even a brief client stall.
# Six ticks preserves continuous coverage with a full six-tick safety margin.
SUMMON_AURA_RENEW_INTERVAL_TICKS = 6

SUMMON_AURA_RESOURCE_BY_TEMPLATE = {
    "minion_combatturret": "skill_turretcombat_minion_buff_aura",
    "minion_teslaturret": "skill_turrettesla_minion_buff_aura",
    "minion_machinegunturret": "skill_turretmachinegun_minion_buff_aura",
    "minion_coveringfireturret": "skill_turretcoveringfire_minion_buff_aura",
}


def summon_startup_seconds(duration, graphics, animation_state, sequence) -> float:
    """Use the same deployment gate for skill and socket-created minions."""
    # StartupDuration is the minimum AI delay. Explicit deployment clips can
    # outlast it: the bundled conex turret Spawn clips are 26 keys at 40 ms.
    # Sequence tracks below hijack the actor; longer sound/VFX tails do not.
    startup = max(0.0, float(duration))
    if "spawn" in str(animation_state).casefold().split(";") and str(graphics) in {
        "characters/dwarf_turret_conex", "characters/dwarf_turret_tesla_conex",
    }:
        startup = max(startup, 1.04)
    frames = {
        "woodwrath_spawn": 25,
        "dwarf_taunt_spawn": 14,
        "minion_nian_cny2026_spawn": 10,
    }.get(str(sequence), 0)
    return max(startup, frames / SERVER_TICKS_PER_SECOND)


@dataclass(frozen=True)
class PlayerSummonAuraDefinition:
    """The short, continuously refreshed aura attached to one construction."""

    resource_id: str
    effect_id: int
    radius: float
    height: float


def load_player_summon_aura(
    database: sqlite3.Connection,
    monster_template_id: str,
) -> PlayerSummonAuraDefinition | None:
    """Resolve a construction aura against the server's local content build."""

    resource_id = SUMMON_AURA_RESOURCE_BY_TEMPLATE.get(
        str(monster_template_id).casefold()
    )
    if resource_id is None:
        return None
    row = database.execute(
        """
        SELECT rowid - 1,
               COALESCE(AuraRadius, 0),
               COALESCE(AuraHeight, 0)
        FROM _Template_StatusEffect
        WHERE Id=? LIMIT 1
        """,
        (resource_id,),
    ).fetchone()
    if row is None:
        return None
    return PlayerSummonAuraDefinition(
        resource_id=resource_id,
        effect_id=int(row[0]),
        radius=max(0.0, float(row[1])),
        height=max(0.0, float(row[2])),
    )


def _aura_instance_id(actor_id: int, ordinal: int) -> int:
    return (
        ((int(actor_id) & 0x000FFFFF) << 12)
        | (0x100 + int(ordinal))
    ) & 0xFFFFFFFF


def player_summon_aura_entry(
    definition: PlayerSummonAuraDefinition,
    *,
    actor_id: int,
    owner_actor_id: int,
    position: tuple[float, float, float],
    yaw: float,
    server_tick: int,
    ordinal: int,
    instance_id: int | None = None,
) -> StatusEffectEntry:
    """Build the generated spherical aura entry observed in retail."""

    start_tick = int(server_tick) & 0xFFFFFFFF
    duration_ticks = SUMMON_AURA_REFRESH_TICKS
    return StatusEffectEntry(
        effect_id=int(definition.effect_id),
        field_04=(
            _aura_instance_id(actor_id, ordinal)
            if instance_id is None
            else int(instance_id) & 0xFFFFFFFF
        ),
        field_0c=(start_tick + duration_ticks) & 0xFFFFFFFF,
        field_14=SERVER_TICKS_PER_SECOND,
        field_08=start_tick,
        field_18=0,
        field_10=duration_ticks,
        field_1c=100,
        field_20=int(owner_actor_id),
        field_25=False,
        field_26=False,
        field_39=False,
        has_details=True,
        values=(0.0, 0.0, 0.0, 0.0, 0.0),
        field_38=True,
        field_3c=2,
        shape_values=(
            float(definition.radius),
            float(definition.height),
            float(yaw),
            *(float(component) for component in position),
        ),
    )


def add_player_summon_aura_to_spawn(
    group: MonsterSpawnGroup,
    definition: PlayerSummonAuraDefinition | None,
    *,
    owner_actor_id: int,
    instance_id: int | None = None,
) -> MonsterSpawnGroup:
    """Add the post-creation aura snapshot to a summon ``NewMonster`` batch."""

    if definition is None:
        return group
    monster_command = next(
        command
        for command in group.spawn_batch.commands
        if isinstance(command, NewMonsterServerCommand)
    )
    monster = monster_command.value
    aura = player_summon_aura_entry(
        definition,
        actor_id=monster.actor_id,
        owner_actor_id=owner_actor_id,
        position=monster.position,
        yaw=monster.field_a0,
        server_tick=(int(monster.field_c4) + 1) & 0xFFFFFFFF,
        ordinal=1,
        instance_id=instance_id,
    )
    commands = list(group.spawn_batch.commands)
    for index, command in enumerate(commands):
        if (
            isinstance(command, StatusEffectServerCommand)
            and int(command.actor_id) == int(monster.actor_id)
            and command.value.clear_existing
        ):
            commands[index] = StatusEffectServerCommand(
                StatusEffect(
                    True,
                    (aura, *command.value.entries),
                ),
                command.actor_id,
            )
            break
    else:
        commands.insert(
            1,
            StatusEffectServerCommand(
                StatusEffect(True, (aura,)),
                monster.actor_id,
            ),
        )
    return replace(
        group,
        spawn_batch=MonsterSpawnBatch(tuple(commands)),
    )


def player_summon_preparation_batch(
    group: MonsterSpawnGroup,
    definition: PlayerSummonAuraDefinition | None,
    *,
    owner_actor_id: int,
    instance_id: int | None = None,
) -> MonsterUpdateBatch:
    """Build retail's MonsterUpdate/FactionInfo/aura preparation batch."""

    monster = next(
        command.value
        for command in group.spawn_batch.commands
        if isinstance(command, NewMonsterServerCommand)
    )
    trailing = ()
    if definition is not None:
        trailing = (
            StatusEffectServerCommand(
                StatusEffect(
                    False,
                    (
                        player_summon_aura_entry(
                            definition,
                            actor_id=monster.actor_id,
                            owner_actor_id=owner_actor_id,
                            position=monster.position,
                            yaw=monster.field_a0,
                            server_tick=monster.field_c4,
                            ordinal=0,
                            instance_id=instance_id,
                        ),
                    ),
                ),
                monster.actor_id,
            ),
        )
    return MonsterUpdateBatch(
        monster=MonsterUpdate(
            int(monster.field_88),
            int(monster.field_90),
            int(monster.field_98),
            0,
            100,
        ),
        monster_actor_id=int(monster.actor_id),
        faction=FactionInfo(monster.placement),
        faction_actor_id=0,
        commands=trailing,
    )


def player_summon_aura_refresh_command(
    definition: PlayerSummonAuraDefinition,
    *,
    actor_id: int,
    owner_actor_id: int,
    position: tuple[float, float, float],
    yaw: float,
    server_tick: int,
    instance_id: int,
) -> StatusEffectServerCommand:
    """Build one rolling aura lease for an already registered construction."""

    return StatusEffectServerCommand(
        StatusEffect(
            False,
            (
                player_summon_aura_entry(
                    definition,
                    actor_id=actor_id,
                    owner_actor_id=owner_actor_id,
                    position=position,
                    yaw=yaw,
                    server_tick=server_tick,
                    ordinal=2,
                    instance_id=instance_id,
                ),
            ),
        ),
        int(actor_id),
    )
