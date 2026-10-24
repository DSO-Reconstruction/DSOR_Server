"""Account-state capture and persistence for the local player."""

from __future__ import annotations

from collections import deque
import sqlite3
import threading
import time
from dso_server.protocol.currency_protocol import normalize_game_currency_balances
from dso_server.protocol.inventory_codec import owned_inventory_items
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


TUTORIAL_LOGIN_HUB = "a0002_start_hub"
TUTORIAL_LOGIN_MAPS = frozenset({
    "a0001_start_tutorial_dun",
    TUTORIAL_LOGIN_HUB,
    "a0003_start_garden_dun",
    "a0004_start_tomb_dun",
    "a0005_start_fortress_dun",
})
# Where a character without a finished tutorial chain must replay the
# tutorial: the dungeon that opens the act-00 chapter (data/quests.xml,
# ``a0001-start-tutorial-01-<class>`` from ``a0001_start_starter``).
TUTORIAL_DUNGEON_MAP = "a0001_start_tutorial_dun"
# Closing any ``a0001-start-tutorial-03-<class>`` is the gate the hub uses
# for ``a0002-WayToGrimmagstone-01-n`` (requiredOperator ANY over the four
# class variants), so having reached that quest is the chain-entry marker.
TUTORIAL_FINAL_QUESTS = (
    "a0001-start-tutorial-03-w",
    "a0001-start-tutorial-03-d",
    "a0001-start-tutorial-03-r",
    "a0001-start-tutorial-03-m",
)


class PlayerPersistenceService:
    """Snapshot, store, and restore the durable local-player account state."""

    def _nearest_login_hub(self, start_map_id: str) -> str:
        """Return the tutorial hub or find a hub through active map exits."""

        start_map_id = str(start_map_id or "")
        # Tutorial exits depend on per-instance quest events, which are absent
        # during login. Searching only open exits fell through to Kingscity;
        # searching locked exits could skip the fortress story to Grimford.
        # Relogging anywhere in the tutorial returns to its own staging hub.
        if start_map_id in TUTORIAL_LOGIN_MAPS:
            return TUTORIAL_LOGIN_HUB
        cache = getattr(self, "_safe_login_hub_cache", None)
        if cache is None:
            cache = {}
            self._safe_login_hub_cache = cache
        if start_map_id in cache:
            return cache[start_map_id]
        queue = deque((start_map_id,))
        visited = set()
        while queue and len(visited) < 256:
            map_id = queue.popleft()
            if not map_id or map_id in visited:
                continue
            visited.add(map_id)
            try:
                if self.static_database.gameplay_map_ruleset(map_id) == 5:
                    cache[start_map_id] = map_id
                    return map_id
                exits = load_local_map_exits(
                    self.static_database_path, map_id
                )
            except (OSError, KeyError, ValueError, sqlite3.Error):
                continue
            for exit_definition in exits:
                destination = str(
                    exit_definition.destination_map_id or ""
                )
                if (
                    exit_definition.active
                    and destination not in visited
                    and map_database_path(
                        self.static_database_path, destination
                    ).is_file()
                ):
                    queue.append(destination)
        cache[start_map_id] = DEFAULT_GAMEPLAY_MAP
        return DEFAULT_GAMEPLAY_MAP

    @staticmethod
    def _sync_inventory_live_health(
        s: Session, inventory=None
    ):
        """Copy authoritative combat HP into an inventory snapshot.

        InventoryInfo carries health for initialization, but it is not the
        live combat owner.  Any inventory delta built from a stale snapshot
        otherwise heals the player when the client applies that delta.
        """

        with s.flow_lock:
            target = s.gameplay_inventory if inventory is None else inventory
            combat = s.gameplay_combat_state
            character = s.gameplay_character
            presence_enabled = s.gameplay_presence_callback is not None
            remote_actor_id = s.gameplay_remote_actor_id
        if target is None or combat is None:
            return target
        actor_id = combat.player_actor_id
        if presence_enabled and character is not None:
            actor_id = (
                remote_actor_id
                or CharacterService._remote_actor_id(character.character_id)
            )
        with combat.lock:
            health = combat.player_health.get(int(actor_id))
            live_health = (
                max(0, int(health[0])) if health is not None else None
            )
        if live_health is None:
            return target
        if inventory is None:
            with s.flow_lock:
                if (
                    s.gameplay_inventory is not target
                    or s.gameplay_combat_state is not combat
                ):
                    return target
                target.health_points = live_health
        else:
            target.health_points = live_health
        return target

    def _tutorial_chain_reached(self, state) -> bool:
        """Whether this character has at least accepted the final tutorial.

        Accepting ``a0001-start-tutorial-03-<class>`` matters as much as
        closing it: the quest's goal is the hub's spellweaver, so a character
        mid-turn-in has to stay on the hub.  Only a character that never
        reached that quest has no turn-in target there and must replay the
        dungeon instead.
        """

        quest_log = getattr(state, "quest_log", None) or {}
        return any(quest_id in quest_log for quest_id in TUTORIAL_FINAL_QUESTS)

    def _persisted_gameplay_map(self, character, fallback: str) -> str:
        store = getattr(self, 'account_store', None)
        if store is None or character is None:
            return fallback
        state = store.get(getattr(character, 'character_id', 0))
        if state is None:
            return fallback
        # A character parked on the tutorial hub before reaching the final
        # tutorial quest has no quest giver there: the hub's first offer
        # (``a0002-WayToGrimmagstone-01-n``) requires a closed
        # ``a0001-start-tutorial-03-<class>``.  Returns from the tutorial
        # dungeons route through ``_nearest_login_hub`` straight into the
        # hub, which stranded exactly those characters outside the chain.
        # Replay the tutorial dungeon until the chain is reached; a character
        # holding the in-progress final quest stays on the hub, its goal NPC
        # is there.
        if not self._tutorial_chain_reached(state):
            current = str(state.map_id or "")
            if current == TUTORIAL_LOGIN_HUB:
                # Hub without a chain entry: send back to the dungeon.
                if map_database_path(
                    self.static_database_path, TUTORIAL_DUNGEON_MAP
                ).is_file():
                    destination = TUTORIAL_DUNGEON_MAP
                    state.map_id = destination
                    state.map_access_id = destination
                    state.map_difficulty = 0
                    state.scaling_difficulty = 0
                    state.position = None
                    store.put(state, flush=True)
                    character.map_id = destination
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] tutorial-repair moved "
                        f"{getattr(character, 'name', state.character_id)!r} "
                        f"from {TUTORIAL_LOGIN_HUB!r} back to "
                        f"{destination!r} (tutorial chain not reached)"
                    )
                    return destination
            elif current == TUTORIAL_DUNGEON_MAP:
                # Mid-chain in the dungeon: keep the character there.  The
                # quest givers and goals of tasks 0..2 are dungeon NPCs, so
                # the safe-login hub fallback would strand the chain.
                return TUTORIAL_DUNGEON_MAP
        if map_database_path(
            self.static_database_path, state.map_id
        ).is_file():
            try:
                hostile = (
                    self.static_database.gameplay_map_ruleset(state.map_id)
                    != 5
                )
            except (AttributeError, KeyError, OSError, ValueError):
                hostile = True
            if not hostile:
                return state.map_id
            destination = self._nearest_login_hub(state.map_id)
            state.map_id = destination
            state.map_access_id = destination
            state.map_difficulty = 0
            state.scaling_difficulty = 0
            state.position = None
            store.put(state, flush=True)
            character.map_id = destination
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] safe-login moved "
                f"{getattr(character, 'name', state.character_id)!r} "
                f"from hostile map to nearest hub {destination!r}"
            )
            return destination
        return fallback

    def _capture_account_state(self, s: Session) -> AccountState | None:
        """Detach a durable snapshot with a minimal ``flow_lock`` critical section.

        Every gameplay thread contends for this session's ``flow_lock``: the
        map's 25 Hz simulation worker reads the owner clock and each viewer's
        spawned set through it, projectile deadline workers commit impacts
        through it, and the movement/skill ingress workers apply positions
        through it. Deep-copying a fully-equipped inventory inside that
        critical section measured 6.5 ms at p50 (251 items) and ran on every
        cast, so one player's persistence directly delayed hit registration and
        monster targeting for **everyone on the map**.

        The critical section is now reference capture plus small container
        copies. The two genuinely deep structures are cloned after the lock is
        released:

        - ``gameplay_inventory`` is copy-on-write. Structural changes
          *replace* it (``InventoryMutationBuilder``, ``equip_inventory_item``
          and ``store_inventory_item`` all return a new object); the only
          in-place writes are the scalar ``health_points`` and
          ``secondary_resource`` fields, and ``_sync_inventory_live_health``
          below overwrites health from authoritative combat state anyway.
          Cloning a snapshot that a concurrent commit has since replaced is
          therefore still internally consistent, and the newer object is
          persisted by the save that follows that commit.
        - ``gameplay_character`` carries only scalars and strings. Its few
          in-place writers (``map_id``, ``premium_currency``) already ran
          without this lock before this change, so holding it here never
          ordered them; a clone observes one field value or the other.
        - ``gameplay_event_progress`` is also copy-on-write:
          ``_advance_event_progress`` mutates a private copy and assigns the
          result, so the live mapping is never edited in place.
        """

        with s.flow_lock:
            live_character = s.gameplay_character
            live_inventory = s.gameplay_inventory
            currency = s.gameplay_currency_state
            vendor_buyback = s.gameplay_vendor_buyback
            admin_booking_ids = tuple(getattr(s, "gameplay_admin_booking_ids", ()))
            if (
                live_character is None
                or live_inventory is None
                or currency is None
            ):
                return None
            map_id = str(s.gameplay_map or getattr(
                live_character, 'map_id', DEFAULT_GAMEPLAY_MAP
            ))
            last_move = s.gameplay_last_move
            spawn_position = s.gameplay_spawn_position
            encounter = s.gameplay_monster_encounter
            game_currencies = dict(s.gameplay_game_currencies)
            currency_ic = s.gameplay_currency_ic
            next_item_id = s.gameplay_next_item_id
            quickbar_info = s.gameplay_quickbar_info
            # QuickSlotsInfo/QuickSlotInfo are frozen dataclasses holding
            # tuples of frozen dataclasses, so the reference is the snapshot.
            quick_slots = s.gameplay_quick_slots
            timed_offer_purchases = dict(
                s.gameplay_timed_offer_purchases
            )
            live_event_progress = s.gameplay_event_progress
            event_participation = dict(s.gameplay_event_participation)
            quest_log = CharacterService._quest_log_document(s)
            wanted_thief = CharacterService._wanted_thief_document(s)
            season_pass = CharacterService._season_pass_document(s)
            daily_challenges = CharacterService._daily_challenge_document(s)
            unlocked_maps = tuple(sorted(s.gameplay_unlocked_maps))
            admin_all_maps = s.gameplay_admin_all_maps
            achievements = CharacterService._achievement_document(s)
            chapters_unlocked = CharacterService._chapters_unlocked_document(s)
            # MountEffectState and its MountDefinition are both frozen.
            mount_ride = s.gameplay_mount_ride
            map_access_id = str(
                s.gameplay_map_access_id or map_id
            )
            map_difficulty = int(s.gameplay_map_difficulty)
            scaling_difficulty = int(
                s.gameplay_scaling_difficulty
            )
            # StickerbookEntry is frozen; the dict is the only mutable level.
            stickerbook_entries = tuple(
                s.gameplay_stickerbook_entries[key]
                for key in sorted(s.gameplay_stickerbook_entries)
            )
            recipe_book_resources = tuple(
                s.gameplay_recipe_book_resources
            )
            talent_category_points = tuple(
                int(value) for value in s.gameplay_talent_category_points
            )
            active_costume = str(s.gameplay_active_costume)
            hidden_equipment_skins = dict(
                getattr(s, "gameplay_hidden_equipment_skins", {})
            )
            active_pet = str(s.gameplay_active_pet)
            active_mercenary = str(s.gameplay_active_mercenary)
            active_decorations = tuple(
                resource for resource, _instance in s.gameplay_active_decorations.values()
            )
            membership_type = int(s.gameplay_membership_type)
            membership_expires_at = str(s.gameplay_membership_expires_at)
            membership_active = bool(s.gameplay_membership_active)
            # Event and map-scoped buffs are rebuilt from their catalog on
            # every map entry; saving them would outlive the event or carry
            # the arena's PvP ruleset into the hub.
            transient_effect_instance_ids = (
                set(s.gameplay_event_status_effect_instance_ids)
                | set(
                    s.gameplay_fractured_planes_status_effect_instance_ids
                )
                | set(s.gameplay_map_status_effect_instance_ids)
                | {
                    int(entry.field_04)
                    for entry in (
                        getattr(s, "gameplay_mercenary_status_effect", None),
                        getattr(s, "gameplay_mercenary_aura_status_effect", None),
                        *tuple(getattr(
                            s, "gameplay_mercenary_aura_status_effects", ()
                        )),
                    )
                    if entry is not None
                }
            )
            status_effects = {
                instance_id: effect
                for instance_id, effect
                in s.gameplay_persistent_status_effects.items()
                if instance_id not in transient_effect_instance_ids
            }
            timed_status_effect_expiries = dict(
                s.gameplay_timed_status_effect_expiries
            )
            # TalentCategorySelection/TalentSelectionEntry are frozen and hold
            # only tuples, so each preset value is already an immutable
            # snapshot; only the surrounding mappings need rebuilding.
            talent_categories = tuple(s.gameplay_talent_categories)
            skill_talent_presets = dict(s.gameplay_skill_talent_presets)
            category_talent_presets = {
                category: dict(presets)
                for category, presets
                in s.gameplay_category_talent_presets.items()
            }
            talent_effect_instance_ids = {
                instance_id
                for instance_ids in (
                    s.gameplay_talent_effect_instance_ids.values()
                )
                for instance_id in instance_ids
            }
        # Deep structures are detached outside the critical section.
        inventory = clone_inventory_info(live_inventory)
        character = copy.deepcopy(live_character)
        event_progress = copy.deepcopy(live_event_progress)
        CharacterService._sync_inventory_live_health(s, inventory)
        for instance_id in talent_effect_instance_ids:
            status_effects.pop(instance_id, None)
            timed_status_effect_expiries.pop(instance_id, None)
        now_epoch = time.time()
        for instance_id, expires_at in tuple(
            timed_status_effect_expiries.items()
        ):
            if (
                float(expires_at) <= now_epoch
                or instance_id not in status_effects
            ):
                timed_status_effect_expiries.pop(instance_id, None)
                status_effects.pop(instance_id, None)
        # Positive values are live/legacy epoch deadlines. Persist a negative
        # remaining duration for authored non-real-time effects so logging
        # out pauses them without changing the active session's clock.
        consumables = load_consumable_catalog(self.static_database_path)
        for instance_id, expires_at in tuple(
            timed_status_effect_expiries.items()
        ):
            entry = status_effects[instance_id]
            definition = consumables.definition_for_effect(
                entry.effect_id, tuple(entry.values)
            )
            if (
                definition is not None
                and definition.persistent
                and not definition.real_time
            ):
                timed_status_effect_expiries[instance_id] = -max(
                    1e-6, float(expires_at) - now_epoch
                )
        if mount_ride is not None and mount_ride.instance_id not in status_effects:
            mount_effect = mount_status_effect(
                mount_ride,
                actor_id=LOCAL_PLAYER_ACTOR_ID,
                active=True,
            ).entries[0]
            status_effects[mount_effect.field_04] = mount_effect
        position = spawn_position
        if last_move is not None:
            try:
                coordinate_frame = (
                    encounter.coordinate_frame
                    if encounter is not None
                    else load_map_coordinate_frame(
                        self.static_database_path, map_id
                    )
                )
                position = coordinate_frame.wire_to_world(
                    last_move.relative_position
                )
            except (OSError, ValueError):
                pass
        inventory.changed_item_ids = []
        inventory.field_158 = {}
        inventory.field_1e0 = False
        inventory.actor_id = LOCAL_PLAYER_ACTOR_ID
        maximum_item_id = max(
            (item.item_id for item in owned_inventory_items(inventory)),
            default=RUNTIME_ITEM_ID_BASE - 1,
        )
        # Record each effect's template name: login re-resolves it in case a
        # client-data update renumbered _Template_StatusEffect.
        try:
            effect_names = load_status_runtime_catalog(
                self.static_database_path
            ).effect_names
        except (OSError, sqlite3.Error):
            effect_names = {}
        status_effect_names = {
            instance_id: effect_names[int(effect.effect_id)]
            for instance_id, effect in status_effects.items()
            if int(effect.effect_id) in effect_names
        }
        return AccountState(
            character_id=int(character.character_id),
            admin_booking_ids=admin_booking_ids,
            last_visited_town=str(getattr(s, "gameplay_last_visited_town", "")),
            map_id=map_id,
            unlocked_maps=unlocked_maps,
            admin_all_maps=admin_all_maps,
            admin_invulnerable=bool(getattr(s, "gameplay_gm_invulnerable", False)),
            position=(
                tuple(float(value) for value in position)
                if position is not None
                else None
            ),
            inventory=inventory,
            vendor_buyback=copy.deepcopy(vendor_buyback),
            currency=currency,
            currency_ic=int(currency_ic),
            game_currencies=normalize_game_currency_balances(
                game_currencies
            ),
            next_item_id=max(
                int(next_item_id),
                RUNTIME_ITEM_ID_BASE,
                maximum_item_id + 1,
            ),
            quickbar_info=quickbar_info,
            quick_slots=quick_slots,
            timed_offer_purchases=timed_offer_purchases,
            event_progress=event_progress,
            event_participation=event_participation,
            quest_log=quest_log,
            wanted_thief=wanted_thief,
            season_pass=season_pass,
            daily_challenges=daily_challenges,
            achievements=achievements,
            chapters_unlocked=chapters_unlocked,
            status_effects=tuple(
                status_effects[key] for key in sorted(status_effects)
            ),
            status_effect_names=status_effect_names,
            mount_ride=mount_ride,
            talent_categories=talent_categories,
            skill_talent_presets=skill_talent_presets,
            category_talent_presets=category_talent_presets,
            pvp_enabled=bool(s.gameplay_pvp_enabled),
            honor_points=int(getattr(s, "gameplay_honor_points", 0)),
            pvp_matches_won=int(getattr(s, "gameplay_pvp_matches_won", 0)),
            pvp_matches_lost=int(getattr(s, "gameplay_pvp_matches_lost", 0)),
            pvp_kills=int(getattr(s, "gameplay_pvp_kills", 0)),
            pvp_deaths=int(getattr(s, "gameplay_pvp_deaths", 0)),
            honor_points_today=int(getattr(s, "gameplay_honor_points_today", 0)),
            pvp_kills_today=int(getattr(s, "gameplay_pvp_kills_today", 0)),
            pvp_solo_matches_won=int(getattr(s, "gameplay_pvp_solo_matches_won", 0)),
            pvp_solo_matches_played=int(getattr(s, "gameplay_pvp_solo_matches_played", 0)),
            pvp_team_matches_won=int(getattr(s, "gameplay_pvp_team_matches_won", 0)),
            pvp_team_matches_played=int(getattr(s, "gameplay_pvp_team_matches_played", 0)),
            pvp_kills_alone_total=int(getattr(s, "gameplay_pvp_kills_alone_total", 0)),
            pvp_kills_alone_today=int(getattr(s, "gameplay_pvp_kills_alone_today", 0)),
            pvp_kills_group_total=int(getattr(s, "gameplay_pvp_kills_group_total", 0)),
            pvp_kills_group_today=int(getattr(s, "gameplay_pvp_kills_group_today", 0)),
            pvp_kills_battle_total=int(getattr(s, "gameplay_pvp_kills_battle_total", 0)),
            pvp_kills_battle_today=int(getattr(s, "gameplay_pvp_kills_battle_today", 0)),
            pvp_dishonorable_kills=int(getattr(s, "gameplay_pvp_dishonorable_kills", 0)),
            pvp_stats_date=str(getattr(s, "gameplay_pvp_stats_date", "")),
            title_type=int(s.gameplay_title_type),
            achievement_title=str(s.gameplay_achievement_title),
            map_access_id=map_access_id,
            map_difficulty=map_difficulty,
            scaling_difficulty=scaling_difficulty,
            stickerbook_entries=stickerbook_entries,
            active_costume=active_costume,
            hidden_equipment_skins=hidden_equipment_skins,
            active_pet=active_pet,
            active_mercenary=active_mercenary,
            active_decorations=active_decorations,
            membership_type=membership_type,
            membership_expires_at=membership_expires_at,
            membership_active=membership_active,
            recipe_book_resources=recipe_book_resources,
            talent_category_points=talent_category_points,
            timed_status_effect_expiries=(
                timed_status_effect_expiries
            ),
        )

    def _persist_gameplay_session(
            self, s: Session, *, reason: str, flush: bool = False) -> bool:
        """Serialize session snapshots so older captures cannot win a race."""

        with s.flow_lock:
            persist_lock = getattr(s, "gameplay_persist_lock", None)
            if persist_lock is None:
                persist_lock = threading.RLock()
                s.gameplay_persist_lock = persist_lock
        with persist_lock:
            return PlayerPersistenceService._persist_gameplay_session_locked(
                self, s, reason=reason, flush=flush
            )

    def _persist_gameplay_session_locked(
            self, s: Session, *, reason: str, flush: bool = False) -> bool:
        store = getattr(self, 'account_store', None)
        with s.flow_lock:
            transfer_started = s.gameplay_exit_transition_started
        if transfer_started and reason in {
            'client disconnect', 'connection replaced', 'interval'
        }:
            # The source snapshot was flushed immediately before arming the
            # destination. A late disconnect/interval callback from the old
            # socket must not overwrite the destination map and entry position
            # after the new session has initialized.
            return False
        if reason in {"client disconnect", "connection replaced"}:
            CharacterService._quest_leave_map(self, s, notify=False)
        if store is None:
            return False
        CharacterService._refresh_premium_bags(s)
        state = CharacterService._capture_account_state(self, s)
        if state is None:
            return False
        # Runtime persistence normally stays off the packet-handler's disk-I/O
        # path: the store publishes the snapshot in memory and coalesces it on
        # its dedicated writer. On session retirement, wait for the final
        # snapshot to reach SQLite before the client can reconnect (or the
        # process can be restarted) with stale inventory.
        closing_session = reason in {
            "client disconnect", "connection replaced", "server shutdown"
        }
        # Durable inventory mutations and map handoffs pass ``flush=True``.
        # Dropping that flag here left the newest in-memory snapshot queued
        # for the async writer, so a destination server could reload the
        # previous equipment (including empty gem/rune/jewel sockets).
        durable_snapshot = bool(flush or closing_session)
        store.put(state, flush=durable_snapshot)
        # The per-account selection payload embeds the active character's
        # equipment, which this snapshot may have just changed. Its cache key
        # only tracks the roster, so the entry is dropped here rather than
        # letting the selection screen render the gear the player logged in
        # with.
        account_id = getattr(s, 'account_id', None)
        if account_id is not None and hasattr(self, 'character_roster_lock'):
            with self.character_roster_lock:
                cache = getattr(self, '_account_selection_cache', None)
                if cache is not None:
                    cache.pop(account_id, None)
        if hasattr(self, 'character_selection'):
            live = s.gameplay_character
            for character in self.character_selection.characters:
                if character.character_id == state.character_id:
                    character.map_id = state.map_id
                    character.premium_currency = (
                        state.currency.currency_type
                    )
                    # arm_gameplay hands the session a deepcopy of the roster
                    # record, so a level gained in play only exists on that
                    # copy. Carrying it back is what makes the profile written
                    # below hold the new level instead of the one the player
                    # logged in with.
                    if live is not None and int(
                        getattr(live, 'character_id', 0)
                    ) == int(character.character_id):
                        character.level = max(1, int(live.level))
                        character.progression_u32 = int(
                            getattr(live, 'progression_u32', 0)
                        )
                        character.progression_u64 = int(
                            getattr(live, 'progression_u64', 0)
                        )
                    store.put_profile(
                        CharacterProfile.from_character_record(
                            character, gold=state.currency.gold
                        ),
                        flush=durable_snapshot,
                    )
                    break
        if reason != 'interval':
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] queued account state for "
                f"character {state.character_id} ({reason})"
            )
        return True

    @staticmethod
    def _request_account_save(
            s: Session, reason: str, *, flush: bool = False) -> bool:
        """Snapshot the account, coalescing purely regenerative reasons.

        ``_capture_account_state`` detaches the entire account. Combat calls
        this once per cast for skill-resource spend, which on a busy map is
        several times a second per player, and every one of those snapshots
        competes with the map's simulation worker and impact workers for the
        session lock.

        Resource is regenerating state: it is recomputed from live combat on
        the next tick and is bounded by its own maximum, so persisting each
        individual spend buys nothing. Reasons in
        ``COALESCED_ACCOUNT_SAVE_REASONS`` are therefore rate limited to
        ``COALESCED_ACCOUNT_SAVE_INTERVAL_SECONDS``; the 15 s autosave and
        every lifecycle boundary (map transfer, disconnect, retirement) still
        capture the current value. Durable reasons -- anything the player would
        notice losing, such as inventory, talents or currency -- are never
        coalesced, and any caller passing ``flush=True`` bypasses this.
        """

        CharacterService._refresh_premium_bags(s)
        callback = s.gameplay_persist_callback
        if callback is None:
            return False
        if not flush and reason in COALESCED_ACCOUNT_SAVE_REASONS:
            now = time.perf_counter()
            with s.flow_lock:
                elapsed = now - float(s.gameplay_last_coalesced_save_at)
                if elapsed < COALESCED_ACCOUNT_SAVE_INTERVAL_SECONDS:
                    s.gameplay_coalesced_saves_skipped += 1
                    return False
                s.gameplay_last_coalesced_save_at = now
        return bool(callback(s, reason, flush))

    def _player_xp_state(self, character) -> dict:
        """Resolve the normal-XP fields consumed during local actor creation."""

        current_xp = max(0, int(getattr(character, 'progression_u32', 0)))
        # Keep a handler-safe fallback for installations without static.db4.
        # The normal localhost setup replaces it with the shared DB values.
        level_base_xp = 0
        next_level_base_xp = max(1, current_xp + 1)
        database = getattr(self, 'static_database', None)
        if database is not None:
            try:
                level_base_xp, next_level_base_xp = (
                    database.player_xp_bounds(
                        int(getattr(character, 'class_id', 0)),
                        max(1, int(getattr(character, 'level', 1))),
                    )
                )
            except (OSError, ValueError) as exc:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] WARNING: cannot resolve "
                    f"player XP bounds: {exc}; using safe fallback"
                )
        current_xp = min(
            max(current_xp, level_base_xp), next_level_base_xp
        )
        return {
            'current_xp': current_xp,
            'level_base_xp': level_base_xp,
            'next_level_base_xp': next_level_base_xp,
        }

    @staticmethod
    def _merge_quick_slots(
        primary: QuickSlotsInfo | None,
        fallback: QuickSlotsInfo | None,
    ) -> QuickSlotsInfo | None:
        """Merge two QuickSlotsInfo objects, preferring populated slots and preserving consumable shortcuts."""
        if primary is None and fallback is None:
            return None
        if primary is None:
            primary = fallback
            fallback = None

        fallback_bars = fallback.quickbars if fallback else ()
        bar_count = max(len(primary.quickbars), len(fallback_bars))
        merged_bars = []
        for bar_idx in range(bar_count):
            bar_p = primary.quickbars[bar_idx] if bar_idx < len(primary.quickbars) else None
            bar_f = fallback_bars[bar_idx] if bar_idx < len(fallback_bars) else None

            merged_slots = []
            for slot_idx in range(17):
                slot_p = bar_p[slot_idx] if bar_p and slot_idx < len(bar_p) else None
                slot_f = bar_f[slot_idx] if bar_f and slot_idx < len(bar_f) else None

                if slot_p is not None and slot_p.kind != 0xFFFFFFFF:
                    merged_slots.append(slot_p)
                elif slot_f is not None and slot_f.kind != 0xFFFFFFFF:
                    merged_slots.append(slot_f)
                elif slot_p is not None:
                    merged_slots.append(slot_p)
                elif slot_f is not None:
                    merged_slots.append(slot_f)
                else:
                    merged_slots.append(QuickSlotInfo(0xFFFFFFFF, None))
            merged_bars.append(tuple(merged_slots))

        # Ensure common bottombar slots (8..16) are mirrored across all bars
        for slot_idx in range(8, 17):
            populated = next(
                (bar[slot_idx] for bar in merged_bars if bar[slot_idx].kind != 0xFFFFFFFF),
                None,
            )
            if populated is not None:
                for b_idx in range(len(merged_bars)):
                    if merged_bars[b_idx][slot_idx].kind == 0xFFFFFFFF:
                        bar_list = list(merged_bars[b_idx])
                        bar_list[slot_idx] = populated
                        merged_bars[b_idx] = tuple(bar_list)

        # Sanitize slot 8 (Mount only) and slot 9 (Mercenary/Pet only)
        for b_idx in range(len(merged_bars)):
            bar_list = list(merged_bars[b_idx])
            s8 = bar_list[8]
            res8 = str(s8.resource_id or "")
            if s8.kind == 2 and ("mercenary" in res8 or "pet" in res8):
                if bar_list[9].kind == 0xFFFFFFFF:
                    bar_list[9] = s8
                bar_list[8] = QuickSlotInfo(0xFFFFFFFF, None)
            s9 = bar_list[9]
            res9 = str(s9.resource_id or "")
            if s9.kind in (1, 2) and "mount" in res9:
                if bar_list[8].kind == 0xFFFFFFFF:
                    bar_list[8] = s9
                bar_list[9] = QuickSlotInfo(0xFFFFFFFF, None)
            merged_bars[b_idx] = tuple(bar_list)

        return QuickSlotsInfo(tuple(merged_bars))
