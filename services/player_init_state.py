"""Local-player state resolved before the initialization batch is built.

``PlayerInitializationService`` walks these phases in order.  Each one reads
and extends a single :class:`PlayerInitializationState`, so the orchestration
stays readable while the individual derivations keep their original shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import math
import time
from typing import Any

from dso_server.protocol.currency_protocol import (
    GAME_CURRENCY_DEFINITIONS,
    STARTER_GOLD_UNITS,
    normalize_game_currency_balances,
)

from dso_server.runtime.context import *
from dso_server.gameplay.premium_bags import reconcile_premium_bags
from dso_server.protocol.membership_protocol import membership_is_active
from dso_server.protocol.inventory_codec import (
    BOOTSTRAP_ITEM_ID,
    BOOTSTRAP_ITEM_TEMPLATE,
    BOOTSTRAP_SECOND_RUNE_ID,
    owned_inventory_items,
)
from dso_server.gameplay.map_unlocks import (
    restored_map_unlocks, saved_quest_map_unlocks, unlocked_travel_map_ids,
)
from dso_server.runtime.transport import Session
from dso_server.gameplay.status_runtime_catalog import (
    load_status_runtime_catalog,
    rebind_saved_status_effects,
)
from dso_server.gameplay.endless_tower_effects import (
    endless_tower_player_buffs,
)
from dso_server.gameplay.event_system import event_status_entry
from dso_server.gameplay.membership_effects import (
    membership_status_effect,
    membership_status_effect_ids,
)
from dso_server.gameplay.skin_unlocks import repair_skin_source_inventory
from dso_server.services.admin_effects import restore_admin_effects, limited_admin_speed_entries


def normalize_stickerbook_entry(entry):
    """Translate entries emitted by the obsolete favorite-slot encoding.

    Retail uses the first integer as an owned/favorite state (2/3) and keeps
    the second integer zero.  Preserve a legacy favorite if either the old
    synthetic position or the favorite state is present, but never forward
    the synthetic position to the client.
    """

    favorite = int(entry.field_14) >= 3 or int(entry.field_18) > 0
    return replace(
        entry,
        field_14=3 if favorite else 2,
        field_18=0,
    )


def _upgrade_legacy_starter_gold(persisted_state) -> None:
    """Upgrade only an unsaved old starter balance with its bootstrap runes.

    The previous starter snapshot can already be persisted by the time the
    server is updated. Its exact 10,000-gold balance plus one of the old
    server-issued rune IDs identifies that untouched starter state.
    """

    if persisted_state is None or int(persisted_state.currency.amount) not in {
        10_000,
        100_000_000,
    }:
        return
    old_rune_ids = {BOOTSTRAP_ITEM_ID, BOOTSTRAP_SECOND_RUNE_ID}
    has_legacy_starter_rune = any(
        int(item.item_id) in old_rune_ids
        or (
            item.template_id == BOOTSTRAP_ITEM_TEMPLATE
            and int(item.item_level) >= 140
        )
        for item in owned_inventory_items(persisted_state.inventory)
    )
    if has_legacy_starter_rune:
        persisted_state.currency = replace(
            persisted_state.currency, amount=STARTER_GOLD_UNITS
        )


@dataclass
class PlayerInitializationState:
    """Everything one local-player initialization derives before it sends.

    Populated in phase order by ``_resolve_initialization_character``,
    ``_resolve_initialization_inventory``, ``_encode_initialization_new_player``,
    ``_resolve_initialization_talents`` and
    ``_resolve_initialization_persistent_effects``.
    """

    # Character, map and route.
    character: Any
    template_row_index: int
    gameplay_map: str
    persisted_state: Any
    map_access: Any
    spawn_position: Any
    coordinate_frame: Any
    map_exits: tuple
    unlocked_travel_maps: Any
    travel_map_unlocks: tuple
    player_xp_state: dict
    talent_category_points: tuple

    # Inventory.
    inventory: Any = None
    rekeyed_next_item_id: int | None = None

    # NewPlayer.
    game_currency_balances: Any = None
    new_player: Any = None

    # Talent presets.
    talent_categories: tuple = ()
    skill_talent_presets: dict = field(default_factory=dict)
    category_talent_presets: dict = field(default_factory=dict)
    active_skill_preselection: int = 0
    active_skill_selections: tuple = ()

    # Durable per-account state and status effects.
    currency_state: Any = None
    membership_type: int = 0
    membership_expires_at: str = ""
    membership_active: bool = False
    membership_status_effect_ids: frozenset = frozenset()
    mount_ride: Any = None
    persistent_status_effects: dict = field(default_factory=dict)
    timed_status_effect_expiries: dict = field(default_factory=dict)
    talent_effect_instance_ids: dict = field(default_factory=dict)
    # Instance ids of buffs granted by active game events. They live in the
    # persistent set so combat, XP and the client see them, but they are
    # rebuilt from the schedule on every initialization and never saved.
    event_status_effect_instance_ids: frozenset = frozenset()
    fractured_planes_status_effect_instance_ids: frozenset = frozenset()
    next_status_effect_instance_id: int = 0
    item_set_evaluation: Any = None
    item_set_effects: dict = field(default_factory=dict)
    socket_effect_evaluation: Any = None
    socket_effects: dict = field(default_factory=dict)
    maximum_health: int = 1
    maximum_resource: int = 100
    health_regeneration_fraction: float = 0.0
    health_regeneration_absolute: float = 0.0
    health_regeneration_interval: float = 1.0
    resource_regeneration_fraction: float = 0.0
    resource_regeneration_absolute: float = 0.0
    resource_regeneration_interval: float = 1.0
    resource_regeneration_delay: float = 0.0


class PlayerInitializationStateService:
    """Derive and commit the session state one initialization depends on."""

    def _resolve_map_entry_route(
            self, *, character, gameplay_map: str,
            route_spawn_position, persisted_state):
        """Resolve the map access and spawn position one entry will use.

        Initialization and the pre-warm started when the route is armed must
        agree exactly: the monster-encounter cache is keyed on the resolved
        difficulty, monster level, and the component the spawn position lands
        on.  Any disagreement would silently build the population twice.
        """

        route_access_id = str(
            getattr(character, "map_access_id", "") or ""
        )
        route_scaling_difficulty = int(
            getattr(character, "scaling_difficulty", 0) or 0
        )
        if (
            not route_access_id
            and persisted_state is not None
            and persisted_state.map_id == gameplay_map
        ):
            route_access_id = (
                persisted_state.map_access_id or persisted_state.map_id
            )
            route_scaling_difficulty = (
                persisted_state.scaling_difficulty
            )
        map_access = resolve_map_access(
            self.static_database_path,
            route_access_id or gameplay_map,
            player_level=max(1, int(character.level)),
            scaling_difficulty=route_scaling_difficulty,
        )
        spawn_position = (
            route_spawn_position
            if route_spawn_position is not None
            else persisted_state.position
            if (
                persisted_state is not None
                and persisted_state.map_id == gameplay_map
                and persisted_state.position is not None
                and int(getattr(persisted_state, 'map_difficulty', 0)) == map_access.map_difficulty
                and int(getattr(persisted_state, 'scaling_difficulty', 0)) == map_access.scaling_difficulty
            )
            else load_local_spawn_position(
                self.static_database_path, gameplay_map
            )
        )
        return map_access, spawn_position

    def _prewarm_map_encounter(
            self, map_name: str, character, route_spawn_position) -> None:
        """Generate the destination population before the client asks for it.

        Building a dense map costs seconds, and initialization runs on the
        receive thread, where it reads no packets and acknowledges nothing
        until it finishes.  Routes are armed while the client is still
        loading the map, so the work is started here and the cache is warm by
        the time gameplay command 0x006f arrives.  If it is still running,
        initialization waits on that same build instead of starting a second.
        """

        if character is None:
            return

        def warm() -> None:
            try:
                account_store = getattr(self, 'account_store', None)
                persisted_state = (
                    account_store.get(
                        getattr(character, 'character_id', 0)
                    )
                    if account_store is not None
                    else None
                )
                map_access, spawn_position = (
                    CharacterService._resolve_map_entry_route(
                        self,
                        character=character,
                        gameplay_map=map_name,
                        route_spawn_position=route_spawn_position,
                        persisted_state=persisted_state,
                    )
                )
                if map_access.physical_map_id != map_name:
                    return
                active_event_flags = frozenset({
                    *CharacterService._event_catalog(
                        self
                    ).active_event_flags(),
                    *map_access.event_set_flags,
                })
                load_local_monster_encounter(
                    self.static_database_path,
                    map_name,
                    monster_level=map_access.monster_level,
                    scaling_difficulty=map_access.monster_scaling_difficulty,
                    player_position=spawn_position,
                    active_event_flags=active_event_flags,
                )
            except Exception as exc:
                # A pre-warm is an optimization: initialization still builds
                # the population itself, and reports content errors properly.
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] could not pre-warm "
                    f"{map_name}: {type(exc).__name__}: {exc}"
                )

        threading.Thread(
            target=warm,
            name=f"dso-prewarm-{map_name}",
            daemon=True,
        ).start()

    def _resolve_initialization_character(
            self, s: Session) -> PlayerInitializationState:
        """Resolve the character, map access, spawn and progression fields."""

        character = s.gameplay_character
        if character is None and hasattr(self, 'character_selection'):
            character = self.character_selection.active_character
        if character is None:
            character = SimpleNamespace(
                name='Player',
                class_id=0,
                gender=0,
                hair_style=0,
                beard_style=0,
                body_style=0,
                body_stature=0,
                level=1,
            )

        template_row_index = s.gameplay_template_row_index
        if template_row_index is None and hasattr(self, 'player_template_rows'):
            template_row_index = self.player_template_rows.get(
                character.class_id
            )
        if template_row_index is None:
            # This is the first local _Template_Player row in static.db4.
            template_row_index = 0

        gameplay_map = s.gameplay_map or DEFAULT_GAMEPLAY_MAP
        account_store = getattr(self, 'account_store', None)
        booking_delivery = getattr(self, 'admin_booking_delivery', None)
        if booking_delivery is not None:
            booking_delivery.deliver(getattr(character, 'character_id', 0))
        persisted_state = (
            account_store.get(getattr(character, 'character_id', 0))
            if account_store is not None
            else None
        )
        # The bundled XP table includes post-cap rows for compatibility with
        # source data. They are not playable levels on this server. Normalize
        # old/admin-set characters before map scaling and NewPlayer are built.
        try:
            level_cap_xp, _next_level_xp = self.static_database.player_xp_bounds(
                int(character.class_id), 100
            )
            current_level = max(1, int(getattr(character, 'level', 1)))
            current_xp = max(0, int(getattr(character, 'progression_u32', 0)))
            if current_level > 100 or current_xp > level_cap_xp:
                character.level = 100
                character.progression_u32 = level_cap_xp
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] capped character "
                    f"{getattr(character, 'name', character.character_id)!r} "
                    "at level 100"
                )
        except (AttributeError, CharacterSelectionError, OSError, TypeError, ValueError):
            pass
        map_access, spawn_position = (
            CharacterService._resolve_map_entry_route(
                self,
                character=character,
                gameplay_map=gameplay_map,
                route_spawn_position=s.gameplay_route_spawn_position,
                persisted_state=persisted_state,
            )
        )
        if map_access.physical_map_id != gameplay_map:
            raise ValueError(
                f"map access {map_access.request_map_id!r} resolves to "
                f"{map_access.physical_map_id!r}, not active map "
                f"{gameplay_map!r}"
            )
        coordinate_frame = load_map_coordinate_frame(
            self.static_database_path, gameplay_map
        )

        def has_local_destination(definition) -> bool:
            try:
                destination_access = resolve_map_access(
                    self.static_database_path,
                    definition.destination_map_id,
                    player_level=max(1, int(character.level)),
                )
            except ValueError:
                return False
            return map_database_path(
                self.static_database_path,
                destination_access.physical_map_id,
            ).is_file()

        active_event_flags = frozenset({
            *CharacterService._event_catalog(
                self
            ).active_event_flags(),
            *map_access.event_set_flags,
        })
        map_exit_definitions = tuple(
            definition
            for definition in load_local_map_exits(
                self.static_database_path,
                gameplay_map,
                active_event_flags=active_event_flags,
            )
            if (
                definition.active
                and definition.destination_map_id
                and has_local_destination(definition)
            )
        )
        map_exits = tuple(filter_parallel_world_exits_for_access(
            self.static_database_path,
            gameplay_map,
            map_exit_definitions,
            selected_map_access_id=map_access.request_map_id,
        ))
        unlocked_travel_maps = unlocked_travel_map_ids(
            self.static_database_path,
            restored_map_unlocks(persisted_state, (
                gameplay_map, map_access.request_map_id, map_access.access_group_id,
            )) | saved_quest_map_unlocks(self.static_database_path, persisted_state),
            admin_all_maps=bool(getattr(persisted_state, "admin_all_maps", False)),
        )
        travel_map_unlocks = (
            self.static_database.travel_map_unlock_words(
                unlocked_travel_maps
            )
            if self.static_database is not None
            else (0, 0, 0, 0)
        )
        player_xp_state = CharacterService._player_xp_state(
            self, character
        )
        # Index 1 is the wisdom/knowledge progress: it is what reaches the
        # client as ``knowledge_xp``, what wisdom pickups add to, and what
        # gates wisdom drops against ``gameplay_wisdom_maximum_xp``.
        #
        # The cap and the starting value are deliberately separate. Capping a
        # saved value at the starting value would clamp every point earned in
        # play back to that number on the next initialization, which silently
        # discards wisdom the player picked up.
        category_maximums = (
            int(player_xp_state['current_xp']),
            self.talent_database.maximum_progress_xp(1),
            self.talent_database.maximum_progress_xp(2),
            self.talent_database.maximum_progress_xp(3),
        )
        starting_talent_category_points = (
            int(player_xp_state['current_xp']),
            # Wisdom is earned in play, so a character starts with none.
            DEFAULT_WISDOM_PROGRESS_XP,
            self.talent_database.maximum_progress_xp(2),
            self.talent_database.maximum_progress_xp(3),
        )
        persisted_points = tuple(
            getattr(persisted_state, "talent_category_points", ()) or ()
        )
        if len(persisted_points) == 4:
            talent_category_points = tuple(
                min(int(category_maximums[index]), max(0, int(value)))
                for index, value in enumerate(persisted_points)
            )
        else:
            # A "dktest" special case used to cap wisdom here so that one
            # development snapshot kept earnable progress. Every character
            # now starts below the cap, so it no longer had any effect.
            talent_category_points = starting_talent_category_points
        return PlayerInitializationState(
            character=character,
            template_row_index=template_row_index,
            gameplay_map=gameplay_map,
            persisted_state=persisted_state,
            map_access=map_access,
            spawn_position=spawn_position,
            coordinate_frame=coordinate_frame,
            map_exits=map_exits,
            unlocked_travel_maps=unlocked_travel_maps,
            travel_map_unlocks=travel_map_unlocks,
            player_xp_state=player_xp_state,
            talent_category_points=talent_category_points,
        )

    def _resolve_initialization_inventory(
            self, state: PlayerInitializationState,
            session: Session | None = None) -> None:
        """Bootstrap, restore, repair and rekey the local player's inventory."""

        character = state.character
        persisted_state = state.persisted_state
        # An equipment allocation (InventoryAllocation type 1) is the native
        # client trigger for RebuildInventoryUI.  That event constructs the
        # complete FixedArray<UI::Slot> before InventoryWidget can be opened.
        # This command is serialized from typed data and does not replay the
        # captured character's inventory.
        inventory_packet, inventory_bits = encode_bootstrap_inventory_info(
            actor_id=LOCAL_PLAYER_ACTOR_ID,
            class_name=CLASS_NAMES[int(character.class_id)],
            item_level=max(1, int(character.level)),
        )
        inventory = decode_inventory_info(inventory_packet, inventory_bits)
        if persisted_state is not None:
            inventory = copy.deepcopy(persisted_state.inventory)
            inventory.actor_id = LOCAL_PLAYER_ACTOR_ID
        overflowed_starter_ids: list[int] = []
        inventory = repair_bootstrap_class_equipment(
            inventory,
            class_name=CLASS_NAMES[int(character.class_id)],
            item_level=max(1, int(character.level)),
            overflowed=overflowed_starter_ids,
        )
        if overflowed_starter_ids:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] character {character.character_id}: "
                f"inventory full, {len(overflowed_starter_ids)} displaced "
                "starter item(s) wait in overflow until a slot frees up: "
                f"{overflowed_starter_ids}"
            )
        inventory, relocated_essences = repair_ammunition_slot_allocations(
            inventory
        )
        if relocated_essences:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] moved {len(relocated_essences)} "
                "essence(s) out of retired equipment slot 36: "
                f"{relocated_essences}"
            )
        class_name = CLASS_NAMES[int(character.class_id)]
        if class_name == "dwarf":
            inventory, enchanted_item_ids = (
                backfill_missing_equipment_enchantments(
                    self.static_database_path,
                    inventory,
                    class_name=class_name,
                    map_difficulty=state.map_access.map_difficulty,
                )
            )
            if enchanted_item_ids:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] added database-owned "
                    f"enchantments to {len(enchanted_item_ids)} legacy "
                    f"dwarf equipment item(s): {enchanted_item_ids}"
                )
        if FORCE_LOOT_DROPS_FOR_TESTING:
            inventory, promoted_item_ids = (
                promote_local_loot_equipment_levels(
                    inventory,
                    static_database_path=self.static_database_path,
                    target_item_level=max(1, int(character.level)),
                )
            )
            if promoted_item_ids:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] promoted "
                    f"{len(promoted_item_ids)} old forced-loot equipment "
                    f"item(s) to level {max(1, int(character.level))}: "
                    f"{promoted_item_ids}"
                )
        inventory = repair_skin_source_inventory(
            inventory,
            static_database_path=self.static_database_path,
            class_name=CLASS_NAMES[int(character.class_id)],
        )
        refresh_inventory_economy_values(
            self.static_database_path,
            inventory,
        )
        rekeyed_next_item_id = None
        if persisted_state is not None:
            inventory_next_item_id = max(
                RUNTIME_ITEM_ID_BASE,
                int(persisted_state.next_item_id),
                max(
                    (int(item.item_id) for item in owned_inventory_items(inventory)),
                    default=RUNTIME_ITEM_ID_BASE - 1,
                ) + 1,
            )
            if session is not None:
                # Reserve the inventory's entire ID range in the same
                # per-world allocator used for mob and quest drops. Without
                # this reservation, a character entering a busy map could
                # reuse an ID already assigned to a live world item.
                with self.gameplay_world_lock:
                    world_key = CharacterService._gameplay_instance_key(session)
                    first_item_id = max(
                        inventory_next_item_id,
                        int(self.gameplay_world_next_item_ids.get(
                            world_key, RUNTIME_ITEM_ID_BASE
                        )),
                    )
                    inventory, item_id_mapping, rekeyed_next_item_id = (
                        rekey_inventory_items(
                            inventory,
                            first_item_id=first_item_id,
                        )
                    )
                    self.gameplay_world_next_item_ids[world_key] = max(
                        int(self.gameplay_world_next_item_ids.get(
                            world_key, RUNTIME_ITEM_ID_BASE
                        )),
                        int(rekeyed_next_item_id),
                    )
                    map_id = str(session.gameplay_map or '')
                    self.gameplay_world_next_item_ids[map_id] = max(
                        int(self.gameplay_world_next_item_ids.get(
                            map_id, RUNTIME_ITEM_ID_BASE
                        )),
                        int(rekeyed_next_item_id),
                    )
            else:
                first_item_id = inventory_next_item_id
                inventory, item_id_mapping, rekeyed_next_item_id = (
                    rekey_inventory_items(
                        inventory,
                        first_item_id=first_item_id,
                    )
                )
            if item_id_mapping:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] rekeyed "
                    f"{len(item_id_mapping)} inventory item(s) for "
                    f"destination map: {first_item_id}.."
                    f"{rekeyed_next_item_id - 1}"
                )
        state.inventory = inventory
        state.rekeyed_next_item_id = rekeyed_next_item_id

    def _encode_initialization_new_player(
            self, state: PlayerInitializationState) -> None:
        """Serialize the NewPlayer command carrying currencies and progress."""

        character = state.character
        persisted_state = state.persisted_state
        starter_currency = 100_000_000
        _upgrade_legacy_starter_gold(persisted_state)
        game_currency_balances = normalize_game_currency_balances(
            persisted_state.game_currencies
            if persisted_state is not None
            else {
                definition.wire_id: starter_currency
                for definition in GAME_CURRENCY_DEFINITIONS
            }
        )
        premium = (persisted_state.currency.andermant if persisted_state is not None
                   else getattr(character, "premium_currency", 100_000_000))
        store = getattr(self, "account_store", None)
        starter_gold = STARTER_GOLD_UNITS
        if persisted_state is None and store is not None:
            profile = store.get_profile(character.character_id)
            if profile is not None and int(profile.gold) > 0:
                starter_gold = int(profile.gold)
        if store is not None:
            premium = store.currency_for_character(character.character_id,
                CurrencyChanged(premium, 0, (0, 0, 0), 0, False)).andermant
        honor_points = (
            max(0, int(persisted_state.honor_points))
            if persisted_state is not None
            else 0
        )
        title_type = (
            int(persisted_state.title_type)
            if persisted_state is not None
            else 0
        )
        achievement_title = (
            str(persisted_state.achievement_title)
            if persisted_state is not None
            else ""
        )
        pvp_matches_won = max(0, int(getattr(persisted_state, "pvp_matches_won", 0) or 0)) if persisted_state is not None else 0
        pvp_matches_lost = max(0, int(getattr(persisted_state, "pvp_matches_lost", 0) or 0)) if persisted_state is not None else 0
        pvp_kills = max(0, int(getattr(persisted_state, "pvp_kills", 0) or 0)) if persisted_state is not None else 0

        pvp_solo_won = getattr(persisted_state, "pvp_solo_matches_won", None) if persisted_state is not None else None
        pvp_solo_played = getattr(persisted_state, "pvp_solo_matches_played", None) if persisted_state is not None else None
        pvp_solo_matches_won = max(0, int(pvp_solo_won)) if pvp_solo_won is not None and int(pvp_solo_won) > 0 else pvp_matches_won
        pvp_solo_matches_played = max(0, int(pvp_solo_played)) if pvp_solo_played is not None and int(pvp_solo_played) > 0 else (pvp_matches_won + pvp_matches_lost)
        pvp_team_matches_won = max(0, int(getattr(persisted_state, "pvp_team_matches_won", 0) or 0)) if persisted_state is not None else 0
        pvp_team_matches_played = max(0, int(getattr(persisted_state, "pvp_team_matches_played", 0) or 0)) if persisted_state is not None else 0

        kills_alone = getattr(persisted_state, "pvp_kills_alone_total", None) if persisted_state is not None else None
        kills_battle = getattr(persisted_state, "pvp_kills_battle_total", None) if persisted_state is not None else None
        pvp_kills_total = pvp_kills
        pvp_kills_alone_total = max(0, int(kills_alone)) if kills_alone is not None and int(kills_alone) > 0 else pvp_kills
        pvp_kills_group_total = max(0, int(getattr(persisted_state, "pvp_kills_group_total", 0) or 0)) if persisted_state is not None else 0
        pvp_kills_battle_total = max(0, int(kills_battle)) if kills_battle is not None and int(kills_battle) > 0 else pvp_kills
        pvp_dishonorable_kills = max(0, int(getattr(persisted_state, "pvp_dishonorable_kills", 0) or 0)) if persisted_state is not None else 0

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        saved_date = str(getattr(persisted_state, "pvp_stats_date", "") or "") if persisted_state is not None else ""
        if saved_date == today_str and persisted_state is not None:
            honor_points_today = max(0, int(getattr(persisted_state, "honor_points_today", 0) or 0))
            pvp_kills_today = max(0, int(getattr(persisted_state, "pvp_kills_today", 0) or 0))
            pvp_kills_alone_today = max(0, int(getattr(persisted_state, "pvp_kills_alone_today", 0) or 0))
            pvp_kills_group_today = max(0, int(getattr(persisted_state, "pvp_kills_group_today", 0) or 0))
            pvp_kills_battle_today = max(0, int(getattr(persisted_state, "pvp_kills_battle_today", 0) or 0))
        else:
            honor_points_today = 0
            pvp_kills_today = 0
            pvp_kills_alone_today = 0
            pvp_kills_group_today = 0
            pvp_kills_battle_today = 0

        new_player_packet, new_player_bits = encode_new_player(
            character,
            state.template_row_index,
            actor_id=LOCAL_PLAYER_ACTOR_ID,
            position=state.spawn_position,
            travel_map_unlocks=state.travel_map_unlocks,
            premium_currency=max(0, int(premium)),
            gold=(
                max(0, int(persisted_state.currency.amount))
                if persisted_state is not None
                else starter_gold
            ),
            badge_of_honor=(
                max(0, int(persisted_state.currency.badge_of_honor))
                if persisted_state is not None
                else starter_currency
            ),
            legacy_progression_currency=(
                max(0, int(persisted_state.currency.legacy_progression))
                if persisted_state is not None
                else starter_currency
            ),
            draken=(
                max(0, int(persisted_state.currency.draken))
                if persisted_state is not None
                else starter_currency
            ),
            glyph_of_power=(
                max(0, int(persisted_state.currency.glyph_of_power))
                if persisted_state is not None
                else starter_currency
            ),
            game_currencies=game_currency_balances,
            knowledge_xp=state.talent_category_points[1],
            fame_xp=state.talent_category_points[3],
            honor_points=honor_points,
            title_type=title_type,
            achievement_title=achievement_title,
            armament_state=inventory_armament_state(state.inventory),
            pvp_kills_total=pvp_kills_total,
            pvp_kills_today=pvp_kills_today,
            pvp_kills_alone_total=pvp_kills_alone_total,
            pvp_kills_alone_today=pvp_kills_alone_today,
            pvp_kills_group_total=pvp_kills_group_total,
            pvp_kills_group_today=pvp_kills_group_today,
            pvp_kills_battle_total=pvp_kills_battle_total,
            pvp_kills_battle_today=pvp_kills_battle_today,
            pvp_dishonorable_kills=pvp_dishonorable_kills,
            honor_points_today=honor_points_today,
            pvp_solo_matches_played=pvp_solo_matches_played,
            pvp_solo_matches_won=pvp_solo_matches_won,
            pvp_team_matches_played=pvp_team_matches_played,
            pvp_team_matches_won=pvp_team_matches_won,
            **state.player_xp_state,
        )
        new_player, consumed = decode_new_player_prefix(
            new_player_packet, new_player_bits
        )
        if consumed != new_player_bits:
            raise ValueError("generated NewPlayer has trailing bits")
        # NewPlayer +0x388 initializes GamePlayer +0x18, the team used by
        # ClientBattlegroundManager's personal victory/loss lookup. It is
        # independent of both the local actor ID and scoreboard row metadata.
        registry = getattr(self, "_pvp_matchmaking", None)
        match = registry.match_for_character(int(character.character_id)) if registry is not None else None
        if match is not None and match.map_id == state.gameplay_map:
            with match.lock:
                participant = match.participant(int(character.character_id))
                if participant is not None and not participant.left:
                    new_player = replace(new_player, field_388=participant.team)
        state.game_currency_balances = game_currency_balances
        state.new_player = new_player

    def _resolve_initialization_talents(
            self, state: PlayerInitializationState) -> None:
        """Restore the saved talent presets and the active skill layout."""

        persisted_state = state.persisted_state
        talent_categories = (
            copy.deepcopy(persisted_state.talent_categories)
            if persisted_state is not None
            else (
                TalentCategorySelection(
                    ACTIVE_SKILL_TALENT_PRESELECTION, (), ()
                ),
                TalentCategorySelection(0, (), ()),
                TalentCategorySelection(0, (), ()),
                TalentCategorySelection(0, (), ()),
            )
        )
        skill_talent_presets = (
            copy.deepcopy(persisted_state.skill_talent_presets)
            if persisted_state is not None
            else {
                int(talent_categories[0].preselection):
                (
                    talent_categories[0].selected_skills
                    + talent_categories[0].selected_talents
                )
            }
        )
        active_skill_preselection = int(
            talent_categories[0].preselection
        )
        active_skill_selections = tuple(
            skill_talent_presets.setdefault(
                active_skill_preselection,
                (
                    talent_categories[0].selected_skills
                    + talent_categories[0].selected_talents
                ),
            )
        )
        category_talent_presets = (
            copy.deepcopy(persisted_state.category_talent_presets)
            if persisted_state is not None
            else {
                category_id: {
                    int(talent_categories[category_id].preselection):
                    talent_categories[category_id].selected_skills
                }
                for category_id in (1, 2, 3)
            }
        )
        for category_id in (1, 2, 3):
            category = talent_categories[category_id]
            layouts = category_talent_presets.setdefault(category_id, {})
            selections = tuple(layouts.setdefault(
                int(category.preselection),
                category.selected_skills,
            ))
            roots = CharacterService._talent_category_roots(
                self.talent_database, selections
            )
            mutable_categories = list(talent_categories)
            mutable_categories[category_id] = TalentCategorySelection(
                int(category.preselection), selections, roots
            )
            talent_categories = tuple(mutable_categories)
        state.talent_categories = (
            TalentCategorySelection(
                active_skill_preselection,
                active_skill_selections,
                (),
            ),
            talent_categories[1],
            talent_categories[2],
            talent_categories[3],
        )
        state.skill_talent_presets = skill_talent_presets
        state.category_talent_presets = category_talent_presets
        state.active_skill_preselection = active_skill_preselection
        state.active_skill_selections = active_skill_selections

    def _resolve_initialization_persistent_effects(
            self, s: Session, state: PlayerInitializationState) -> None:
        """Rebuild the durable currency, membership and status-effect set."""

        character = state.character
        persisted_state = state.persisted_state
        new_player = state.new_player
        currency_state = (
            persisted_state.currency
            if persisted_state is not None
            else CurrencyChanged(
                new_player.fields_170[6],
                new_player.field_190,
                new_player.fields_198,
                new_player.field_1a8,
                True,
            )
        )
        membership_type = (
            int(persisted_state.membership_type)
            if persisted_state is not None
            else DELUXE_MEMBERSHIP
        )
        membership_expires_at = (
            str(persisted_state.membership_expires_at)
            if persisted_state is not None
            else default_deluxe_expiry()
        )
        membership_active = (
            bool(persisted_state.membership_active)
            if persisted_state is not None else True
        )
        membership_active = membership_is_active(
            membership_type, membership_active, membership_expires_at,
        )
        reconcile_premium_bags(state.inventory, active=membership_active)
        mount_ride = (
            copy.deepcopy(persisted_state.mount_ride)
            if persisted_state is not None
            else s.gameplay_mount_ride
        )
        if mount_ride is not None:
            sticker_definition = load_sticker_definition(
                self.static_database_path,
                mount_ride.definition.stickerbook_resource_id,
            )
            if sticker_definition is not None and sticker_definition.mount is not None:
                # Effect indexes refreshed from the active database, with
                # the captured timing (stickerbook_state).
                resolved_mount = sticker_definition.mount
                mount_ride = replace(
                    mount_ride,
                    definition=resolved_mount,
                    effect_id=resolved_mount.ride_effect_id,
                    # Every map initialization resets the chase clock to 0.
                    # A saved ride's start tick belongs to the previous map
                    # (or server process), so carry the active ride forward
                    # on the new map's clock instead of replaying a stale tick.
                    start_tick=0,
                )
        # Saved wire ids are _Template_StatusEffect row numbers; re-resolve
        # them by the recorded name in case the client data was updated.
        persistent_status_effects = {
            effect.field_04: effect
            for effect in (
                rebind_saved_status_effects(
                    persisted_state.status_effects,
                    getattr(persisted_state, "status_effect_names", {}),
                    load_status_runtime_catalog(self.static_database_path),
                )
                if persisted_state is not None
                else ()
            )
        }
        if mount_ride is not None:
            saved_ride_effect = persistent_status_effects.get(
                mount_ride.instance_id
            )
            if saved_ride_effect is not None:
                persistent_status_effects[mount_ride.instance_id] = replace(
                    saved_ride_effect,
                    effect_id=mount_ride.definition.ride_effect_id,
                )
        persistent_status_effects = restore_admin_effects(
            persistent_status_effects,
            load_status_runtime_catalog(self.static_database_path),
        )
        now_epoch = time.time()
        timed_status_effect_expiries = {}
        for instance_id, stored_expiry in (
            persisted_state.timed_status_effect_expiries.items()
            if persisted_state is not None else ()
        ):
            expires_at = float(stored_expiry)
            if expires_at < 0.0:
                expires_at = now_epoch + abs(expires_at)
            if expires_at > now_epoch:
                timed_status_effect_expiries[int(instance_id)] = expires_at
        # Rebase each remaining duration against this map's fresh tick origin.
        persistent_status_effects = {
            instance_id: (
                replace(
                    effect,
                    field_0c=max(
                        1,
                        int(math.ceil(
                            (timed_status_effect_expiries[instance_id]
                             - now_epoch) * SERVER_TICKS_PER_SECOND
                        )),
                    ),
                    field_08=0,
                    field_10=max(
                        1,
                        int(math.ceil(
                            (timed_status_effect_expiries[instance_id]
                             - now_epoch) * SERVER_TICKS_PER_SECOND
                        )),
                    ),
                )
                if instance_id in timed_status_effect_expiries
                else effect
            )
            for instance_id, effect in persistent_status_effects.items()
            if (
                instance_id not in (
                    getattr(
                        persisted_state,
                        "timed_status_effect_expiries",
                        {},
                    ) if persisted_state is not None else {}
                )
                or instance_id in timed_status_effect_expiries
            )
        }
        if mount_ride is not None:
            mount_effect = mount_status_effect(
                mount_ride,
                actor_id=LOCAL_PLAYER_ACTOR_ID,
                active=True,
            ).entries[0]
            persistent_status_effects[mount_effect.field_04] = mount_effect
        # Talent effects are derived from every active category preset. Old
        # saves made before presets were modeled may contain one effect per
        # click; remove those legacy instances and reconstruct the compact
        # retail shape for skill, wisdom, group and honor talents.
        active_talent_selections = tuple(
            (category_id, selection)
            for category_id, category in enumerate(state.talent_categories)
            for selection in category.selected_skills
        )
        talent_effect_wire_ids = set()
        for _category_id, selection in active_talent_selections:
            definition = self.talent_database.definition(
                selection.resource_id
            )
            for status_effect_id in (
                definition.base_status_effect_id,
                definition.stack_status_effect_id,
            ):
                if status_effect_id:
                    talent_effect_wire_ids.add(
                        self.talent_database.status_effect_wire_id(
                            status_effect_id
                        )
                    )
        persistent_status_effects = {
            instance_id: effect
            for instance_id, effect in persistent_status_effects.items()
            if effect.effect_id not in talent_effect_wire_ids
        }
        membership_effect_ids = membership_status_effect_ids(
            self.static_database_path
        )
        persistent_status_effects = {
            instance_id: effect
            for instance_id, effect in persistent_status_effects.items()
            if effect.effect_id not in membership_effect_ids
        }
        next_status_effect_instance_id = max(
            s.gameplay_next_status_effect_instance_id,
            (
                mount_ride.instance_id + 1
                if mount_ride is not None
                else 0
            ),
            (
                max(persistent_status_effects) + 1
                if persistent_status_effects
                else 0
            ),
        )
        if membership_active:
            membership_effect = membership_status_effect(
                self.static_database_path,
                membership_type,
                instance_id=next_status_effect_instance_id,
                actor_id=LOCAL_PLAYER_ACTOR_ID,
                expires_at=membership_expires_at,
                server_tick=0,
            )
            if membership_effect is not None:
                persistent_status_effects[membership_effect.field_04] = membership_effect
                next_status_effect_instance_id += 1
        if persisted_state is not None:
            for resource_id in (
                persisted_state.active_costume,
                persisted_state.active_pet,
                *persisted_state.active_decorations,
            ):
                if not resource_id:
                    continue
                # A costume worn straight from the bag is stored by its item
                # template, which may not be a sticker-book collectible.
                definition = load_cosmetic_definition(
                    self.static_database_path, resource_id
                )
                if definition is None:
                    continue
                matching_instance_id = next(
                    (
                        instance_id
                        for instance_id, effect
                        in persistent_status_effects.items()
                        if effect.effect_id
                        == definition.status_effect_wire_id
                    ),
                    None,
                )
                if matching_instance_id is not None:
                    # Cosmetic statuses are active for the new map too, but
                    # their saved start tick was measured on the old map's
                    # clock. Rebase it just like newly reconstructed effects.
                    persistent_status_effects[matching_instance_id] = replace(
                        persistent_status_effects[matching_instance_id],
                        field_08=0,
                    )
                    continue
                effect = sticker_status_entry(
                    definition,
                    instance_id=next_status_effect_instance_id,
                    start_tick=0,
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                    active=True,
                )
                persistent_status_effects[effect.field_04] = effect
                next_status_effect_instance_id += 1
        # Active game events grant durable buffs (the XP bonus event's
        # ``xpgain,$0:0.25`` for example). They are rebuilt from the schedule
        # here and never persisted, so an event that ended while the player
        # was away leaves nothing behind; one that starts or ends while they
        # are online takes effect at their next map entry or relog.
        event_status_effect_instance_ids = set()
        try:
            event_catalog = CharacterService._event_catalog(self)
        except Exception as exc:
            # A broken schedule must not keep a player out of the map.
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] WARNING: cannot resolve event "
                f"status effects: {type(exc).__name__}: {exc}"
            )
            event_catalog = None
        if event_catalog is not None:
            scheduled_effect_ids = frozenset(
                spec.effect_id
                for spec in event_status_effect_specs(
                    event_catalog,
                    self.static_database_path,
                    include_inactive=True,
                )
            )
            persistent_status_effects = {
                instance_id: effect
                for instance_id, effect in persistent_status_effects.items()
                if effect.effect_id not in scheduled_effect_ids
            }
            for spec in event_status_effect_specs(
                event_catalog,
                self.static_database_path,
                player_level=max(1, int(getattr(character, "level", 1))),
            ):
                effect = event_status_entry(
                    spec,
                    instance_id=next_status_effect_instance_id,
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                )
                persistent_status_effects[effect.field_04] = effect
                event_status_effect_instance_ids.add(effect.field_04)
                next_status_effect_instance_id += 1
        fractured_planes_status_effect_instance_ids = set()
        tower_buffs = endless_tower_player_buffs(
            self.static_database_path,
            state.map_access.request_map_id,
            character_id=int(character.character_id),
        )
        # These run-specific buffs are rebuilt on every floor entry. Remove
        # any stale saved tower effects first, then restore the acts reached
        # by this Fractured Planes run.
        stale_tower_instance_ids = {
            instance_id
            for instance_id, effect in persistent_status_effects.items()
            if int(effect.effect_id) in tower_buffs.all_effect_ids
        }
        persistent_status_effects = {
            instance_id: effect
            for instance_id, effect in persistent_status_effects.items()
            if int(effect.effect_id) not in tower_buffs.all_effect_ids
        }
        timed_status_effect_expiries = {
            instance_id: expires_at
            for instance_id, expires_at in timed_status_effect_expiries.items()
            if instance_id not in stale_tower_instance_ids
        }
        if (
            bool(getattr(character, "fractured_planes_run", False))
            and tower_buffs.act_number > 0
        ):
            for spec in tower_buffs.specs:
                effect = event_status_entry(
                    spec,
                    instance_id=next_status_effect_instance_id,
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                )
                persistent_status_effects[effect.field_04] = effect
                fractured_planes_status_effect_instance_ids.add(
                    effect.field_04
                )
                next_status_effect_instance_id += 1
            if tower_buffs.specs:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] restored Fractured Planes "
                    f"act {tower_buffs.act_number} player effects: "
                    f"{len(tower_buffs.specs)}"
                )
        collector_owned = {
            entry.resource_id
            for entry in (
                persisted_state.stickerbook_entries
                if persisted_state is not None else ()
            )
        }
        collector_transition = reconcile_collector_effects(
            self.static_database_path,
            collector_owned,
            persistent_status_effects,
            actor_id=LOCAL_PLAYER_ACTOR_ID,
            next_instance_id=next_status_effect_instance_id,
            server_tick=0,
        )
        persistent_status_effects = collector_transition.effects
        next_status_effect_instance_id = (
            collector_transition.next_instance_id
        )
        talent_effect_instance_ids = {}
        for category_id, selection in active_talent_selections:
            definition = self.talent_database.definition(
                selection.resource_id
            )
            effects = self.talent_database.status_effect_entries(
                definition,
                target_rank=int(selection.field_00),
                actor_id=LOCAL_PLAYER_ACTOR_ID,
                first_instance_id=next_status_effect_instance_id,
                server_tick=0,
            )
            effect_key = CharacterService._talent_effect_key(
                category_id, selection.resource_id
            )
            talent_effect_instance_ids[effect_key] = tuple(
                effect.field_04 for effect in effects
            )
            for effect in effects:
                persistent_status_effects[effect.field_04] = effect
            next_status_effect_instance_id += len(effects)
        item_set_evaluation = None
        item_set_effects = {}
        if self.item_set_database is not None:
            item_set_evaluation = (
                self.item_set_database.evaluate_inventory(
                    state.inventory,
                    class_name=CLASS_NAMES[int(character.class_id)],
                )
            )
            item_set_transition = (
                self.item_set_database.reconcile_effects(
                    item_set_evaluation,
                    {},
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                    next_instance_id=next_status_effect_instance_id,
                    server_tick=0,
                )
            )
            item_set_effects = item_set_transition.effects
            next_status_effect_instance_id = (
                item_set_transition.next_instance_id
            )
        socket_effect_evaluation = None
        socket_effects = {}
        if self.socket_effect_database is not None:
            socket_effect_evaluation = (
                self.socket_effect_database.evaluate_inventory(
                    state.inventory
                )
            )
            socket_transition = (
                self.socket_effect_database.reconcile_effects(
                    socket_effect_evaluation,
                    {},
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                    next_instance_id=next_status_effect_instance_id,
                    server_tick=0,
                )
            )
            socket_effects = socket_transition.effects
            next_status_effect_instance_id = (
                socket_transition.next_instance_id
            )
        state.currency_state = currency_state
        state.membership_type = membership_type
        state.membership_expires_at = membership_expires_at
        state.membership_active = membership_active
        state.membership_status_effect_ids = membership_effect_ids
        state.mount_ride = mount_ride
        state.persistent_status_effects = persistent_status_effects
        state.timed_status_effect_expiries = timed_status_effect_expiries
        state.talent_effect_instance_ids = talent_effect_instance_ids
        state.event_status_effect_instance_ids = frozenset(
            event_status_effect_instance_ids
        )
        state.fractured_planes_status_effect_instance_ids = frozenset(
            fractured_planes_status_effect_instance_ids
        )
        state.next_status_effect_instance_id = next_status_effect_instance_id
        state.item_set_evaluation = item_set_evaluation
        state.item_set_effects = item_set_effects
        state.socket_effect_evaluation = socket_effect_evaluation
        state.socket_effects = socket_effects

    def _resolve_initialization_vitals(
            self, state: PlayerInitializationState) -> None:
        """Separate persisted current vitality from calculated maxima."""

        character = state.character
        modifier_sources = (
            tuple(state.persistent_status_effects.values())
            + tuple(state.item_set_effects.values())
            + tuple(state.socket_effects.values())
            + tuple(getattr(state.item_set_evaluation, "modifiers", ()))
        )
        stats = calculate_player_combat_stats(
            self.static_database_path,
            class_name=CLASS_NAMES[int(character.class_id)],
            level=max(1, int(character.level)),
            inventory=state.inventory,
            modifier_sources=modifier_sources,
        )
        for entry in limited_admin_speed_entries(
            state.persistent_status_effects.values(),
            load_status_runtime_catalog(self.static_database_path), stats,
        ):
            state.persistent_status_effects[entry.field_04] = entry
        state.maximum_health = max(1, int(stats.maximum_health))
        state.maximum_resource = max(1, int(stats.maximum_resource))
        state.health_regeneration_fraction = max(
            0.0, float(stats.health_regeneration_fraction)
        )
        state.health_regeneration_absolute = max(
            0.0, float(stats.health_regeneration_absolute)
        )
        state.health_regeneration_interval = max(
            0.05, float(stats.health_regeneration_interval)
        )
        state.resource_regeneration_fraction = float(
            stats.resource_regeneration_fraction
        )
        state.resource_regeneration_absolute = float(
            stats.resource_regeneration_absolute
        )
        state.resource_regeneration_interval = max(
            0.05, float(stats.resource_regeneration_interval)
        )
        state.resource_regeneration_delay = max(
            0.0, float(stats.resource_regeneration_delay)
        )
        try:
            ruleset = self.static_database.gameplay_map_ruleset(
                state.gameplay_map
            )
        except (AttributeError, KeyError, OSError, ValueError):
            ruleset = (
                5
                if state.gameplay_map in {"a0200_kingscity", "b1200_hub"}
                else 1
            )
        if int(ruleset) == 5:
            # Town entry is a native full-rest boundary for both pools.
            state.inventory.health_points = float(state.maximum_health)
            state.inventory.secondary_resource = float(
                state.maximum_resource
            )
        else:
            state.inventory.health_points = float(min(
                state.maximum_health,
                max(1, int(round(state.inventory.health_points))),
            ))
            state.inventory.secondary_resource = float(min(
                state.maximum_resource,
                max(0, int(round(state.inventory.secondary_resource))),
            ))

    def _apply_initialization_state(
            self, s: Session, state: PlayerInitializationState) -> None:
        """Commit the resolved snapshot onto the session under one lock."""

        persisted_state = state.persisted_state
        with s.flow_lock:
            s.gameplay_admin_booking_ids = (
                persisted_state.admin_booking_ids if persisted_state is not None else ()
            )
            s.gameplay_gm_invulnerable = bool(
                persisted_state.admin_invulnerable
                if persisted_state is not None else False
            )
            s.gameplay_spawn_position = state.spawn_position
            s.gameplay_coordinate_frame = state.coordinate_frame
            s.gameplay_map_exits = state.map_exits
            s.gameplay_exit_inside_guids = set()
            s.gameplay_exit_distances = {}
            s.gameplay_exit_callback = None
            s.gameplay_map_access_id = state.map_access.request_map_id
            s.gameplay_map_access_group_id = (
                state.map_access.access_group_id
            )
            s.gameplay_fractured_planes_run = bool(
                getattr(state.character, "fractured_planes_run", False)
            )
            s.gameplay_fractured_planes_deadline = getattr(
                state.character, "fractured_planes_deadline", None
            )
            s.gameplay_map_difficulty = state.map_access.map_difficulty
            s.gameplay_scaling_difficulty = (
                state.map_access.scaling_difficulty
            )
            s.gameplay_monster_level = state.map_access.monster_level
            s.gameplay_exit_transition_started = False
            s.gameplay_inventory = state.inventory
            s.gameplay_vendor_buyback = copy.deepcopy(persisted_state.vendor_buyback) if persisted_state is not None else ()
            s.gameplay_currency_store = getattr(self, "account_store", None)
            s.gameplay_currency_state = state.currency_state
            s.gameplay_game_currencies = dict(state.game_currency_balances)
            s.gameplay_currency_ic = (
                persisted_state.currency_ic
                if persisted_state is not None
                else 0
            )
            s.gameplay_next_item_id = max(
                RUNTIME_ITEM_ID_BASE,
                (
                    persisted_state.next_item_id
                    if persisted_state is not None
                    else 0
                ),
                state.rekeyed_next_item_id or 0,
                max(
                    (item.item_id for item in owned_inventory_items(state.inventory)),
                    default=0,
                ) + 1,
            )
            s.gameplay_world_loot = {}
            s.gameplay_announced_world_item_ids = set()
            s.gameplay_quickbar_info = (
                persisted_state.quickbar_info
                if persisted_state is not None
                else QuickbarInfo(1, 0)
            )
            route_quick_slots = getattr(state.character, "route_quick_slots", None)
            persisted_quick_slots = (
                persisted_state.quick_slots
                if persisted_state is not None
                else None
            )
            if isinstance(route_quick_slots, QuickSlotsInfo):
                if isinstance(persisted_quick_slots, QuickSlotsInfo):
                    s.gameplay_quick_slots = CharacterService._merge_quick_slots(
                        route_quick_slots, persisted_quick_slots
                    )
                else:
                    s.gameplay_quick_slots = copy.deepcopy(route_quick_slots)
            elif isinstance(persisted_quick_slots, QuickSlotsInfo):
                s.gameplay_quick_slots = copy.deepcopy(persisted_quick_slots)
            else:
                s.gameplay_quick_slots = None
            s.gameplay_membership_type = state.membership_type
            s.gameplay_membership_expires_at = state.membership_expires_at
            s.gameplay_membership_active = state.membership_active
            s.gameplay_membership_status_effect_ids = (
                state.membership_status_effect_ids
            )
            s.gameplay_timed_offer_purchases = (
                dict(persisted_state.timed_offer_purchases)
                if persisted_state is not None
                else {}
            )
            CharacterService._restore_event_state(s, persisted_state)
            CharacterService._restore_quest_state(s, persisted_state)
            CharacterService._restore_wanted_thief_state(s, persisted_state)
            CharacterService._restore_season_pass_state(s, persisted_state)
            CharacterService._restore_daily_challenge_state(s, persisted_state)
            CharacterService._restore_map_unlocks(self, s, persisted_state)
            CharacterService._restore_achievement_state(s, persisted_state)
            CharacterService._restore_pvp_career(s, persisted_state)
            s.gameplay_title_type = (
                int(persisted_state.title_type)
                if persisted_state is not None
                else 0
            )
            s.gameplay_achievement_title = (
                str(persisted_state.achievement_title)
                if persisted_state is not None
                else ""
            )
            s.gameplay_mount_ride = state.mount_ride
            stickerbook_entries = {
                entry.resource_id: normalize_stickerbook_entry(entry)
                for entry in (
                    persisted_state.stickerbook_entries
                    if persisted_state is not None else ()
                )
            }
            s.gameplay_stickerbook_entries = stickerbook_entries
            s.gameplay_recipe_book_resources = tuple(
                persisted_state.recipe_book_resources
                if (
                    persisted_state is not None
                    and persisted_state.recipe_book_resources
                )
                else load_recipe_book_resources(
                    self.static_database_path
                )
            )
            s.gameplay_active_costume = (
                persisted_state.active_costume
                if persisted_state is not None
                else ""
            )
            s.gameplay_hidden_equipment_skins = (
                dict(persisted_state.hidden_equipment_skins)
                if persisted_state is not None
                else {}
            )
            # Older saves may have the durable HideSkin selection but still
            # have ItemInfo's render-skin gate enabled. Reconcile the item on
            # load so the local player's first appearance snapshot uses the
            # same hidden state as presence and character selection.
            hidden_skins = s.gameplay_hidden_equipment_skins
            if hidden_skins and s.gameplay_inventory is not None:
                items_by_id = {
                    int(item.item_id): item
                    for item in s.gameplay_inventory.items
                }
                for item_id, slots in s.gameplay_inventory.equipment_slots.items():
                    item = items_by_id.get(int(item_id))
                    if item is None:
                        continue
                    item_skin = str(item.field_f8 or item.field_80 or "")
                    for equipment_slot in slots:
                        customization_slot = (
                            12 if int(equipment_slot) == 7
                            else int(equipment_slot)
                        )
                        hidden_skin = str(
                            hidden_skins.get(customization_slot, "")
                        )
                        if hidden_skin and (
                            not item_skin or item_skin == hidden_skin
                        ):
                            item.field_3e = False
                            break
            s.gameplay_active_pet = (
                persisted_state.active_pet
                if persisted_state is not None
                else ""
            )
            s.gameplay_active_mercenary = (
                persisted_state.active_mercenary
                if persisted_state is not None
                else ""
            )
            s.gameplay_mercenary_restore_pending = bool(
                s.gameplay_active_mercenary
            )
            decoration_resources = list(
                persisted_state.active_decorations if persisted_state is not None else ()
            )
            # Older saves stored decorative effects in the costume slot.
            legacy_costume = (
                load_sticker_definition(self.static_database_path, s.gameplay_active_costume)
                if s.gameplay_active_costume else None
            )
            if legacy_costume is not None and legacy_costume.kind == "decoration":
                decoration_resources.append(s.gameplay_active_costume)
                s.gameplay_active_costume = ""
            s.gameplay_active_decorations = {}
            for resource_id in decoration_resources:
                definition = load_sticker_definition(self.static_database_path, resource_id)
                if definition is None or definition.kind != "decoration":
                    continue
                matching = tuple(
                    entry.field_04 for entry in state.persistent_status_effects.values()
                    if entry.effect_id == definition.status_effect_wire_id
                )
                group = definition.exclusive_group.casefold() or definition.resource_id
                s.gameplay_active_decorations[group] = (
                    resource_id, max(matching) if matching else None,
                )
            for resource_id, attribute in (
                (
                    s.gameplay_active_costume,
                    "gameplay_active_costume_effect_instance_id",
                ),
                (
                    s.gameplay_active_pet,
                    "gameplay_active_pet_effect_instance_id",
                ),
            ):
                definition = (
                    load_sticker_definition(
                        self.static_database_path, resource_id
                    )
                    if resource_id
                    else None
                )
                matching = (
                    tuple(
                        entry.field_04
                        for entry in state.persistent_status_effects.values()
                        if (
                            definition is not None
                            and entry.effect_id
                            == definition.status_effect_wire_id
                        )
                    )
                )
                setattr(
                    s,
                    attribute,
                    max(matching) if matching else None,
                )
            s.gameplay_persistent_status_effects = (
                state.persistent_status_effects
            )
            s.gameplay_timed_status_effect_expiries = dict(
                state.timed_status_effect_expiries
            )
            s.gameplay_event_status_effect_instance_ids = frozenset(
                state.event_status_effect_instance_ids
            )
            s.gameplay_fractured_planes_status_effect_instance_ids = (
                frozenset(state.fractured_planes_status_effect_instance_ids)
            )
            s.gameplay_map_status_effect_instance_ids = frozenset()
            s.gameplay_item_set_effects = state.item_set_effects
            s.gameplay_item_set_evaluation = state.item_set_evaluation
            s.gameplay_socket_effects = state.socket_effects
            s.gameplay_socket_effect_evaluation = (
                state.socket_effect_evaluation
            )
            s.gameplay_socket_event_state = {}
            s.gameplay_runtime_status_effects = {}
            s.gameplay_victim_status_effect_instances = {}
            s.gameplay_talent_categories = state.talent_categories
            s.gameplay_skill_talent_presets = state.skill_talent_presets
            s.gameplay_category_talent_presets = (
                state.category_talent_presets
            )
            s.gameplay_talent_category_points = state.talent_category_points
            s.gameplay_wisdom_maximum_xp = (
                self.talent_database.maximum_progress_xp(1)
            )
            s.gameplay_talent_effect_instance_ids = (
                state.talent_effect_instance_ids
            )
            s.gameplay_next_status_effect_instance_id = (
                state.next_status_effect_instance_id
            )
