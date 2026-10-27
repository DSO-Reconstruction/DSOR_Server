"""Hit flag bit order against the client's HitCommand decoder.

release261_1024 (0x14097b0f0) reads, right after the hit tags, +0x48 blocked,
+0x49 critical and +0x4a rift hit; its handler shows "floatingBlock",
"floatingCritical" and "floatingRift" for them. patch5 has only the first two.
"""

from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.combat_protocol import (
    CombatBatch,
    Hit,
    HitServerCommand,
    decode_combat_batch,
    encode_combat_batch,
)


def _hit(*, blocked=False, critical=False):
    return Hit(
        server_tick=1234, hit_tags=(3,), field_48=blocked, field_49=critical,
        current_health=50, maximum_health=100, absorbed_damage=0,
        shield_damage=0, damage=50, damage_type=3, attacker_actor_id=1,
        source_actor_id=1, skill_id=0, impact_strength=0.0, field_80=False,
        field_81=False,
    )


def _flags(hit, release261):
    payload, bits = encode_combat_batch(
        CombatBatch((HitServerCommand(hit, 0x100A7),)),
        legacy_hit_tag_boundary_bit=release261,
    )
    reader = BitReader(payload, bits)
    assert reader.read_uint8() == 0x85
    reader.read_bytes(2)
    assert reader.read_uint32_le() == 1234
    assert tuple(reader.read_uint8() for _ in range(reader.read_uint32_le())) == (3,)
    flags = tuple(reader.read_bool() for _ in range(3 if release261 else 2))
    assert reader.read_uint64_le() == 50
    decoded = decode_combat_batch(
        payload, bits, legacy_hit_tag_boundary_bit=release261
    ).commands[0]
    assert decoded == HitServerCommand(hit, 0x100A7)
    return flags


def test_release261_critical_hit_sets_the_client_critical_flag():
    assert _flags(_hit(critical=True), True) == (False, True, False)


def test_release261_block_sets_the_client_block_flag():
    assert _flags(_hit(blocked=True), True) == (True, False, False)


def test_patch5_layout_keeps_two_flags():
    assert _flags(_hit(critical=True), False) == (False, True)
    assert _flags(_hit(blocked=True), False) == (True, False)
