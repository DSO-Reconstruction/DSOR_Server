"""Each creature's own health and damage, from the client's monster table."""

from dsor.monsters import MONSTERS, monster


def test_the_tutorial_creature_has_twenty_four_health_not_twelve():
    """The twelve this server served was a misreading, and the note said as much.

    It came from decoding a recorded blow as "victim health 0, max 12, damage 11".
    The caution written beside that reading warned that misreading the field one or
    two bits late gives 24 -- and 24 is what the client's own database holds. The
    mirage warned about was the value actually being served.
    """
    creature = monster("a0001_gen_anderworld_creature")
    assert creature.hit_points == 24
    assert creature.level == 1


def test_the_creature_blow_is_one_not_eight():
    """"les monstres ne tapent pas comme sur le vrai serveur" was exactly this.

    Eight was served for every creature alike: eight times too hard for the creature
    the dungeon is full of, and not hard enough for its champion.
    """
    assert monster("a0001_gen_anderworld_creature").max_damage == 1.0
    openexit = monster("a0001_champion_anderworld_creature_openexit")
    assert (openexit.min_damage, openexit.max_damage) == (2.0, 3.0)
    mage = monster("a0001_champion_undead_mage_01")
    assert (mage.min_damage, mage.max_damage, mage.hit_points) == (5.0, 9.0, 50)


def test_the_practice_dummy_is_harmless_by_its_own_template():
    """A second, independent confirmation of something already fixed.

    The phantom damage at spawn was traced to a0001_tutorial_movement_target, and the
    fix was that its only skill is monster_selfkill. Its monster row agrees from a
    different direction: two health, no damage, and the client calls it a "hint
    monster".
    """
    dummy = monster("a0001_tutorial_movement_target")
    assert dummy.harmless
    assert (dummy.hit_points, dummy.min_damage, dummy.max_damage) == (2, 0.0, 0.0)
    assert dummy.name == "hint monster"
    assert not monster("a0001_gen_anderworld_creature").harmless


def test_a_blueprint_the_table_does_not_carry_returns_none():
    """The map's spawn table names one the monster table does not.

    A caller must cope with None rather than assume every spawned blueprint is
    described.
    """
    assert monster("a0001_normal_anderworld_minion_01") is None
    assert monster(None) is None
    assert monster("nothing_of_the_sort") is None


def test_resistances_parse_out_of_the_packed_string():
    creature = monster("a0001_gen_anderworld_creature")
    assert creature.armor == 625.0
    assert creature.resists["Fire"] == 625.0
    # Dark magic is doubled for the anderworld creatures, which is the one asymmetry.
    assert creature.resists["DarkMagic"] == 1251.0
    assert set(creature.resists) == {
        "Fire", "Ice", "Lightning", "Poison", "DarkMagic",
    }


def test_the_world_gives_every_mapped_creature_its_own_numbers():
    import server
    from dsor.mapdata import servable_points
    from dsor.recorded import monster_library

    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    points = servable_points(set(monster_library()))
    service.world.populate_from_map(points, 0.0)
    assert service.world.creatures, "the map placed creatures"

    seen = set()
    for actor, creature in service.world.creatures.items():
        known = monster(creature.blueprint)
        if known is None:
            continue
        seen.add(creature.blueprint)
        assert creature.max_health == float(known.hit_points), creature.blueprint
        assert creature.health == creature.max_health
        blow = service.world.creature_blow(actor)
        assert blow == (known.min_damage + known.max_damage) / 2.0

    # And they are genuinely not all the same, which is the point.
    healths = {
        c.max_health for c in service.world.creatures.values()
    }
    assert len(healths) > 1, f"every creature got the same health: {healths}"
    assert 50.0 in healths, "the undead mage champion"
    assert 2.0 in healths, "the practice dummy"


def test_a_forced_figure_still_overrides_every_template():
    """The debug console must still be able to make a creature take twenty blows."""
    import server

    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    world = service.world
    world.rules.mob_max_health = 500.0
    assert world.creature_health("a0001_gen_anderworld_creature", 0.0) == 500.0
    world.rules.mob_max_health = 0.0
    assert world.creature_health("a0001_gen_anderworld_creature", 0.0) == 24.0
    # And an unknown blueprint with no fallback lands on the tutorial creature's own.
    assert world.creature_health("nothing_of_the_sort", 0.0) == 24.0


def test_negative_creature_damage_turns_retaliation_off():
    """Zero used to mean "off" and now means "ask the template", so off moved."""
    import server

    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    assert service.rules.creature_damage == 0.0, "the default asks the template"
    service.rules.creature_damage = -1.0
    sender = ("127.0.0.1", 1)
    service.world.player(sender).position = None
    service.world.creature_swing(sender)  # must return without touching anything
    assert not service.world.pending_hits


def test_every_creature_the_map_can_spawn_is_in_the_table_or_knowingly_absent():
    from dsor.mapdata import MONSTER_SKILLS

    absent = {bp for bp in MONSTER_SKILLS if monster(bp) is None}
    # Exactly one, and it is named here so a second one shows up as a failure rather
    # than silently falling back.
    assert absent == {"a0001_normal_anderworld_minion_01"}


def test_the_table_is_filtered_to_the_served_map():
    """7985 creatures is 1.3 MB of generated Python. This is the a0001 subset.

    The same choice dsor/mapdata.py makes, with the same consequence: serving a new
    map means regenerating with its prefix.
    """
    assert all(key.startswith("a0001") for key in MONSTERS)
    assert 10 < len(MONSTERS) < 100
