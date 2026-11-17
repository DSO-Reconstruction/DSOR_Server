"""Player traps must catch hostile players, not only monsters.

The trap poll resolves monster trips from the combat world; hostile players
live in the presence layer, so they are tested separately and their damage
goes through the PvP hit pipeline.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.runtime.transport import Session
from dso_server.services import character_server  # noqa: F401  (facade)
from dso_server.services.base import CharacterService


PORT = iter(range(52900, 53000))


def session():
    s = Session(("127.0.0.1", next(PORT)), Mock())
    s.gameplay_pvp_enabled = True
    s.gameplay_inventory = InventoryInfo()
    return s


class Frame:
    """World and wire coordinates share one axis-aligned frame."""

    @staticmethod
    def wire_to_world(position):
        return (float(position[0]), float(position[1]), float(position[2]))

    @staticmethod
    def world_to_wire(position):
        return (float(position[0]), float(position[1]), float(position[2]))


def combat():
    return SimpleNamespace(
        encounter=SimpleNamespace(coordinate_frame=Frame()),
    )


def peer(hostile=True):
    owner = SimpleNamespace(gameplay_combat_state=None)
    return owner


def test_hostile_players_inside_the_trip_radius_are_caught():
    s = session()
    near, far = peer(), peer()
    near.gameplay_combat_state = far.gameplay_combat_state = None
    targets = (
        (11, (1.0, 0.0, 1.0), near),
        (12, (40.0, 0.0, 40.0), far),
    )
    s.gameplay_presence_callback = (
        lambda session, event: targets if event == ("combat_targets",) else None
    )
    world = combat()
    near.gameplay_combat_state = far.gameplay_combat_state = world

    with patch.object(
        CharacterService, "_pvp_target_is_hostile", return_value=True
    ):
        victims = CharacterService._player_trap_victims(
            s, world, (0.0, 0.0, 0.0), 2.0,
        )

    assert victims == (11,)


def test_own_team_and_out_of_combat_players_are_ignored():
    s = session()
    teammate = peer()
    outsider = peer()
    other_combat = SimpleNamespace()
    teammate.gameplay_combat_state = outsider.gameplay_combat_state = other_combat
    targets = (
        (21, (0.5, 0.0, 0.5), teammate),
        (22, (0.5, 0.0, 0.5), outsider),
    )
    s.gameplay_presence_callback = (
        lambda session, event: targets if event == ("combat_targets",) else None
    )
    world = combat()

    def hostile(_session, owner):
        return owner is outsider

    with patch.object(
        CharacterService, "_pvp_target_is_hostile", side_effect=hostile
    ):
        # Both peers are registered against another combat world, so neither
        # belongs to this trap's map.
        assert CharacterService._player_trap_victims(
            s, world, (0.0, 0.0, 0.0), 2.0,
        ) == ()

    outsider.gameplay_combat_state = world
    with patch.object(
        CharacterService, "_pvp_target_is_hostile", side_effect=hostile
    ):
        assert CharacterService._player_trap_victims(
            s, world, (0.0, 0.0, 0.0), 2.0,
        ) == (22,)


def test_a_tripped_trap_sends_its_damage_through_the_pvp_pipeline():
    s = session()
    trap = SimpleNamespace(
        skill_index=1887,
        cast_id=42,
        yaw=0.5,
        server_tick=1000,
        position=(3.0, 0.0, 4.0),
        owner_actor_id=7,
        player_damage_range=(100.0, 200.0),
        critical_hit_chance=0.25,
        critical_damage_multiplier=2.0,
        rift_force_echo_chance=0.0,
        rift_force_echo_multiplier=0.0,
        modifier_sources=(),
        definition=SimpleNamespace(victim_status_effects=(), trip_radius=2.5),
    )
    prepare = Mock(return_value=())
    with patch.object(
        CharacterService, "_prepare_pvp_skill_hits", prepare,
    ):
        timers = CharacterService._player_trap_pvp_hits(
            s, combat(), trap, (11, 12), now=5.0,
        )

    assert timers == ()
    kwargs = prepare.call_args.kwargs
    assert kwargs["source_actor_id"] == 7
    assert kwargs["explicit_target_actor_ids"] == (11, 12)
    assert kwargs["player_damage_range"] == (100.0, 200.0)
    assert prepare.call_args.args[2].skill_index == 1887
