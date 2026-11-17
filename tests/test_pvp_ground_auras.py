"""Player skill ground auras must also land on enemy players.

`apply_player_location_auras` / `explode_player_location_aura` walk the
monster population only, so a cast-time ground aura (Blade Dance's burning
ground) and an explosion (Explosive Arrow) reach players through the PvP
ground strike instead.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from dso_server.runtime.transport import Session
from dso_server.services import character_server  # noqa: F401  (facade)
from dso_server.services.base import CharacterService


PORT = iter(range(53100, 53200))


def session():
    s = Session(("127.0.0.1", next(PORT)), Mock())
    s.connected = True
    s.gameplay_chase_generation = 3
    s.gameplay_pvp_enabled = True
    s.gameplay_remote_actor_id = 1
    return s


def test_a_bullet_explosion_strikes_enemy_players():
    s = session()
    combat = SimpleNamespace(
        lock=__import__("threading").RLock(),
        explode_player_location_aura=Mock(return_value=((), ())),
    )
    s.gameplay_combat_state = combat
    explosion = SimpleNamespace(effect_id=9001, aura_radius=3.5)
    receivers = (SimpleNamespace(resource_id="explosion_damage"),)
    strike = Mock()

    with patch.object(
        CharacterService, "_schedule_pvp_ground_strike", strike,
    ):
        CharacterService._resolve_bullet_explosion(
            s, 3, ("player-explosion", 1), combat, explosion, receivers,
            position=(1.0, 2.0, 3.0), owner_actor_id=1, skill_index=1818,
            player_damage_range=(100.0, 200.0), server_tick=555,
        )

    strike.assert_called_once()
    args, kwargs = strike.call_args
    assert args[2] is explosion
    assert args[3] == tuple(receivers)
    assert kwargs["position"] == (1.0, 2.0, 3.0)
    assert kwargs["damage_range"] == (100.0, 200.0)
    assert kwargs["server_tick"] == 555


def test_the_pvp_strike_is_skipped_when_pvp_is_off():
    s = session()
    s.gameplay_pvp_enabled = False
    s.gameplay_presence_callback = None
    combat = SimpleNamespace(
        encounter=SimpleNamespace(
            coordinate_frame=SimpleNamespace(wire_to_world=lambda value: value)
        ),
    )
    aura = SimpleNamespace(effect_id=9001, aura_radius=3.5)
    # No callback means no targets; the call must not raise.
    CharacterService._schedule_pvp_ground_strike(
        s, combat, aura, (), position=(0.0, 0.0, 0.0),
        damage_range=(10.0, 10.0), server_tick=1,
    )
