"""Mystic Cubes: member-only drops, difficulty tiers and one-of contents."""

from datetime import datetime, timedelta, timezone
import random
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.chest_loot_protocol import (
    _MYSTIC_CUBE_PREMIUM_UNIQUES,
    _MYSTIC_CUBE_SET_PIECES,
    _MYSTIC_CUBE_SHARED_UNIQUES,
    _MYSTIC_CUBE_TIERS,
    ChestContents,
    ChestReward,
    open_mapped_chest,
    roll_chest_contents,
)
from dso_server.protocol.commerce_protocol import load_purchase_item_definition
from dso_server.protocol.gameplay_protocol import (
    InventoryItemAction,
    encode_inventory_item_action,
)
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.membership_protocol import (
    DELUXE_MEMBERSHIP,
    PREMIUM_MEMBERSHIP,
)
from dso_server.protocol.player_init_protocol import (
    TALENT_CATEGORY_POINTS_INFO_COMMAND,
    CurrencyChanged,
    decode_server_command_batch,
)
from dso_server.protocol.world_loot_protocol import (
    MYSTIC_CUBE_DROP_CHANCE,
    generate_monster_loot,
    mystic_cube_template_for_difficulty,
    prime_world_loot_cache,
)
from dso_server.services.loot_presence import mystic_cube_drop_is_eligible
from dso_server.tests import test_achievements as achievement_fixtures


TIERS = {
    "mystic_cube_common": ("mystic_cube_common", 19, 99, 5, ("a", "b", "c")),
    "mystic_cube_uncommon": ("mystic_cube_uncommon", 50, 199, 5, ("b", "c", "d")),
    "mystic_cube_magic": ("mystic_cube_magic", 199, 299, 10, ("d", "e", "f")),
    "mystic_cube_rare": (
        "mystic_cube_rare", 499, 499, 15, ("d", "e", "f", "g", "h"),
    ),
    "mystic_cube_epic": (
        "mystic_cube_epic", 999, 999, 20, ("e", "f", "g", "h", "i", "j"),
    ),
}


class _FixedRng:
    """A ``random.Random`` stand-in: one percentage roll, scripted choices."""

    def __init__(self, roll, *choices):
        self._roll = int(roll)
        self._choices = list(choices)

    def randrange(self, stop):
        assert int(stop) == 100
        return self._roll

    def choice(self, sequence):
        value = self._choices.pop(0) if self._choices else sequence[0]
        assert value in sequence, f"{value!r} is not one of {sequence!r}"
        return value


def _contents(box, *, roll, class_name="mage", choices=()):
    return roll_chest_contents(
        DEFAULT_STATIC_DB_PATH,
        box,
        player_level=1,
        class_name=class_name,
        rng=_FixedRng(roll, *choices),
    )


@pytest.mark.parametrize("difficulty,tier", [
    (0, "mystic_cube_common"),
    (1, "mystic_cube_common"),
    (2, "mystic_cube_uncommon"),
    (3, "mystic_cube_uncommon"),
    (4, "mystic_cube_magic"),
    (5, "mystic_cube_magic"),
    (6, "mystic_cube_rare"),
    (7, "mystic_cube_rare"),
    (8, "mystic_cube_epic"),
    (9, "mystic_cube_epic"),
    # Out-of-range runtime difficulties clamp at the ladder's ends.
    (10, "mystic_cube_epic"),
    (-1, "mystic_cube_common"),
])
def test_a_kills_difficulty_picks_the_cube_tier(difficulty, tier):
    assert mystic_cube_template_for_difficulty(difficulty) == tier


def test_mystic_cubes_drop_at_three_percent():
    assert MYSTIC_CUBE_DROP_CHANCE == 0.03


def test_only_a_live_membership_qualifies_for_the_drop():
    now = datetime.now(timezone.utc)
    future = (now + timedelta(days=5)).isoformat()
    past = (now - timedelta(seconds=5)).isoformat()

    def member(membership_type, active, expires_at, premium=""):
        return SimpleNamespace(
            gameplay_membership_type=membership_type,
            gameplay_membership_active=active,
            gameplay_membership_expires_at=expires_at,
            gameplay_membership_premium_expires_at=premium,
        )

    assert mystic_cube_drop_is_eligible(member(PREMIUM_MEMBERSHIP, True, future))
    assert mystic_cube_drop_is_eligible(member(DELUXE_MEMBERSHIP, True, future))
    # A paused Premium under Deluxe is stored in its own expiry.
    assert mystic_cube_drop_is_eligible(
        member(DELUXE_MEMBERSHIP, True, future, premium=future)
    )
    assert not mystic_cube_drop_is_eligible(
        member(DELUXE_MEMBERSHIP, True, past)
    )
    assert not mystic_cube_drop_is_eligible(
        member(PREMIUM_MEMBERSHIP, False, future)
    )
    assert not mystic_cube_drop_is_eligible(SimpleNamespace())


@pytest.fixture(scope="module")
def cache():
    return prime_world_loot_cache(DEFAULT_STATIC_DB_PATH)


@pytest.fixture(scope="module")
def monster(cache):
    return next(
        key for key, profile in cache.monster_profiles.items()
        if profile.rank_kind == "monster"
    )


def _cube_drops(monster, *, difficulty, eligible):
    generation = generate_monster_loot(
        DEFAULT_STATIC_DB_PATH,
        monster,
        class_name="ranger",
        player_level=60,
        monster_level=60,
        position=(0.0, 0.0, 0.0),
        next_item_id=1,
        rng=random.Random(7),
        force_drops=True,
        member_mystic_cube_eligible=eligible,
        map_difficulty=difficulty,
    )
    return [
        drop.item.template_id for drop in generation.drops
        if drop.item.template_id.startswith("mystic_cube")
    ]


@pytest.mark.parametrize("difficulty,tier", [
    (0, "mystic_cube_common"),
    (2, "mystic_cube_uncommon"),
    (4, "mystic_cube_magic"),
    (6, "mystic_cube_rare"),
    (8, "mystic_cube_epic"),
])
def test_a_member_kill_drops_its_tier_of_cube(monster, difficulty, tier):
    assert _cube_drops(monster, difficulty=difficulty, eligible=True) == [tier]


def test_a_non_member_kill_drops_no_cube(monster):
    assert _cube_drops(monster, difficulty=8, eligible=False) == []


def test_the_authored_tier_amounts_and_gem_ladders():
    for box, (_, gold, glyph, wisdom, ranks) in TIERS.items():
        tier = _MYSTIC_CUBE_TIERS[box]
        assert (tier.gold, tier.glyph, tier.wisdom, tier.gem_ranks) == (
            gold, glyph, wisdom, ranks,
        )


@pytest.mark.parametrize("box", TIERS)
def test_each_category_boundary_of_a_tier(box):
    _, gold, glyph, wisdom, ranks = TIERS[box]
    for roll, expected in (
        (14, "unique"),
        (15, "set"),
        (36, "set"),
        (37, ("money_vc_gold", gold)),
        (51, ("money_vc_gold", gold)),
        (52, ("money_gc", wisdom)),
        (66, ("money_gc", wisdom)),
        (67, ("money_uc_no_auto_use", glyph)),
        (81, ("money_uc_no_auto_use", glyph)),
        (82, "gem"),
        (96, "gem"),
        (97, ("money_rc", 999)),
        (99, ("money_rc", 999)),
    ):
        contents = _contents(box, roll=roll)
        assert contents.source == "mystic_cube"
        [reward] = contents.rewards
        template = reward.definition.template_id
        if expected == "unique":
            assert template in (
                _MYSTIC_CUBE_SHARED_UNIQUES + _MYSTIC_CUBE_PREMIUM_UNIQUES["mage"]
            )
        elif expected == "set":
            assert template in _MYSTIC_CUBE_SET_PIECES["mage"]
        elif expected == "gem":
            family, rank = template.split("_")[1:]
            assert family in {"amethyst", "cyanit", "diamond", "onyx", "ruby"}
            assert rank in ranks
        else:
            assert (template, reward.amount) == expected


@pytest.mark.parametrize("box,ranks", [
    (box, values[4]) for box, values in TIERS.items()
])
def test_a_gem_roll_only_uses_the_tiers_ranks(box, ranks):
    for rank in ranks:
        [reward] = _contents(
            box, roll=82, choices=(rank, "ruby")
        ).rewards
        assert reward.definition.template_id == f"gem_ruby_{rank}"
    # The tier's own choice list is what the roller draws from.
    excluded = next(
        rank for rank in "abcdefghijklmnopqrs" if rank not in ranks
    )
    with pytest.raises(AssertionError):
        _contents(box, roll=82, choices=("ruby", excluded))


def test_the_unique_pool_is_the_shared_pets_and_the_players_premium_trader():
    for class_name, premium in _MYSTIC_CUBE_PREMIUM_UNIQUES.items():
        pool = _MYSTIC_CUBE_SHARED_UNIQUES + premium
        assert "epicmount_jumong_horse_01" in pool
        assert len(set(pool)) == len(pool)
        for template in pool:
            assert load_purchase_item_definition(
                DEFAULT_STATIC_DB_PATH, template
            ).template_id == template
        other_premium = {
            template
            for other, items in _MYSTIC_CUBE_PREMIUM_UNIQUES.items()
            if other != class_name
            for template in items
        }
        assert not other_premium & set(pool)

    piece = _MYSTIC_CUBE_PREMIUM_UNIQUES["ranger"][0]
    [reward] = _contents(
        "mystic_cube_epic", roll=0, class_name="ranger", choices=(piece,),
    ).rewards
    assert reward.definition.template_id == piece
    with pytest.raises(AssertionError):
        _contents(
            "mystic_cube_epic", roll=0, class_name="ranger",
            choices=("warrior_unique_2h_mace_premium_01",),
        )


@pytest.mark.parametrize("class_name", sorted(_MYSTIC_CUBE_SET_PIECES))
def test_a_set_roll_gives_a_piece_of_the_players_class(class_name):
    pieces = _MYSTIC_CUBE_SET_PIECES[class_name]
    assert all(piece.endswith("_mysticcube_set_02") for piece in pieces)
    [reward] = _contents(
        "mystic_cube_common", roll=15, class_name=class_name,
        choices=(pieces[3],),
    ).rewards
    assert reward.definition.template_id == pieces[3]
    other = next(
        piece
        for other_class, items in _MYSTIC_CUBE_SET_PIECES.items()
        if other_class != class_name
        for piece in items
    )
    with pytest.raises(AssertionError):
        _contents(
            "mystic_cube_common", roll=15, class_name=class_name,
            choices=(other,),
        )


def test_a_cube_needs_a_player_class():
    with pytest.raises(ValueError, match="valid player class"):
        roll_chest_contents(
            DEFAULT_STATIC_DB_PATH, "mystic_cube_common",
            player_level=1, class_name="", rng=_FixedRng(37),
        )


def _open(box, *, roll, currency, class_name="mage", choices=()):
    contents = _contents(
        box, roll=roll, class_name=class_name, choices=choices,
    )
    inventory = InventoryInfo(
        items=[ItemInfo(item_id=51, template_id=box, amount=1)],
        storage_slots={51: 0},
    )
    mutation = open_mapped_chest(
        inventory, currency, {}, contents, 51,
        static_database_path=DEFAULT_STATIC_DB_PATH,
        next_item_id=52,
        player_level=1,
        class_name=class_name,
    )
    remaining = [item.template_id for item in mutation.inventory.items]
    return mutation, remaining


def _currency():
    return CurrencyChanged(10, 20, (1, 2, 3), 4, True)


def test_opening_for_gold_pays_gold():
    mutation, remaining = _open(
        "mystic_cube_epic", roll=37, currency=_currency(),
    )
    assert remaining == []
    assert mutation.currency.gold == 20 + 999
    assert mutation.wisdom_amount == 0
    assert mutation.currency_changed


def test_opening_for_glyph_credits_the_glyph_field():
    mutation, remaining = _open(
        "mystic_cube_common", roll=67, currency=_currency(),
    )
    assert remaining == []
    assert mutation.currency.glyph_of_power == 4 + 99
    assert mutation.currency.gold == 20
    assert mutation.currency_changed


def test_opening_for_wisdom_holds_the_points_for_the_caller():
    mutation, remaining = _open(
        "mystic_cube_epic", roll=52, currency=_currency(),
    )
    assert remaining == []
    assert mutation.wisdom_amount == 20
    assert mutation.currency == _currency()
    assert not mutation.currency_changed


def test_opening_for_andermant_pays_999():
    mutation, remaining = _open(
        "mystic_cube_common", roll=97, currency=_currency(),
    )
    assert remaining == []
    assert mutation.currency.andermant == 10 + 999


def test_opening_for_gems_or_uniques_materializes_the_item():
    mutation, remaining = _open(
        "mystic_cube_rare", roll=82, currency=_currency(),
        choices=("g", "ruby"),
    )
    assert remaining == ["gem_ruby_g"]
    assert mutation.currency == _currency()

    mutation, remaining = _open(
        "mystic_cube_common", roll=0, currency=_currency(),
        choices=("epicmount_jumong_horse_01",),
    )
    assert remaining == ["epicmount_jumong_horse_01"]


def test_opening_a_wisdom_cube_raises_the_knights_knowledge():
    case = achievement_fixtures.AchievementServiceTests()
    case.setUp()
    try:
        server, session = case.server, case.session
        box = "mystic_cube_epic"
        session.gameplay_character.level = 1
        session.gameplay_inventory = InventoryInfo(
            items=[ItemInfo(item_id=51, template_id=box, amount=1)],
            storage_slots={51: 0},
        )
        session.gameplay_next_item_id = 52
        session.gameplay_currency_state = _currency()
        session.gameplay_talent_category_points = (0, 10, 0, 0)
        session.gameplay_wisdom_maximum_xp = 100
        # The fixture's split sender drops the packet's exact bit length, so
        # keep it here: the batch carries no padding of its own.
        batches = []

        def _send(payload, **kwargs):
            data = payload[0] if isinstance(payload, tuple) else payload
            batches.append((
                data,
                int(kwargs.get("total_bit_length") or len(data) * 8),
            ))
            return data

        session.send_reliable_ordered_split = _send
        contents = ChestContents(
            box_template_id=box,
            source="mystic_cube",
            rewards=(ChestReward(
                load_purchase_item_definition(
                    DEFAULT_STATIC_DB_PATH, "money_gc"
                ),
                20,
                fixed_currency="wisdom",
            ),),
        )
        payload, _ = encode_inventory_item_action(
            InventoryItemAction(51, 0x13, 0, 0, 0)
        )
        with patch(
            "dso_server.services.inventory_map.roll_chest_contents",
            return_value=contents,
        ), patch.object(
            type(server), "_persist_gameplay_session", return_value=False,
        ):
            server._handle_inventory_item_action(session, payload)
        assert session.gameplay_talent_category_points == (0, 30, 0, 0)
        assert not session.gameplay_inventory.items
        commands = [
            command
            for data, bits in batches if data[0] == 0x85
            for command in decode_server_command_batch(data, bits)
        ]
        [points] = [
            command.value for command in commands
            if command.command_id == TALENT_CATEGORY_POINTS_INFO_COMMAND
        ]
        assert points.values == (0, 30, 0, 0)
        assert points.category == 0xFF
    finally:
        case.doCleanups()
