"""Earned achievements, shared account progress, and durable item rewards.

Retail reports every progress change with one
``Achievements::AchievementInfoCommand`` record and the client raises its
own popup when a record carries a completion timestamp
(docs/QUEST_PROTOCOL.md §11).  Both the progress counters and the
completion stamps live in ``AccountState.achievements`` so a later login
replays the same records (same timestamps, no fresh popups), and
``AccountState.chapters_unlocked`` remembers which ``ChapterUnlocked``
notifications the character already received.

Account-scoped records additionally live in the shared account achievement
table. Its receipt and the recipient character inventory are written in one
SQLite transaction; historical completions retain their unlocks without
retroactively granting items for development-era free achievements.

World happenings reach the service through the same hooks the quest
service uses: ``_on_map_entered`` (Enter_Map), ``_on_monsters_killed``
(Kill), ``_handle_prop_interaction`` (Interact), the presence callback's
``item_collected`` (Pick_Up), ``_quest_turn_in`` (Quest) and
``_award_experience`` (Class_Level).  Points and UnlockAchievements rows
are derived from the completed set after every completion.

Kills also carry ``<template>:<difficulty>`` for the authored difficulty
rows.  UnlockSkin counts the persisted collected-skin set
(``gameplay/skin_collection.py``) and VeteranLevelUnlock the account's
veteran rank (``gameplay/veteran_levels.py``); both are reconciled on map
entry.  HighScoreRanking is fed by the high-score settlement with the final
place; PvP_Match by battleground results.
"""

from __future__ import annotations

from contextlib import nullcontext
import sqlite3
import traceback

from dso_server.runtime.context import *
from dso_server.runtime import progression_queue
from dso_server.runtime.transport import Session
from dso_server.gameplay.achievement_content import (
    AchievementCatalog,
    AchievementChange,
    AchievementProgress,
    apply_achievement_event,
    load_achievement_catalog,
    refresh_derived_achievements,
)
from dso_server.gameplay.skin_collection import held_skins, load_skin_catalog
from dso_server.gameplay.veteran_levels import load_veteran_rules, veteran_level
from dso_server.gameplay.quest_content import announced_chapter
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.player_init_protocol import (
    ACHIEVEMENT_INFO_COMMAND,
    MESSAGE_COMMAND,
    Achievement,
    AchievementInfo,
    ServerCommand,
    encode_server_command_batch,
)
from dso_server.protocol.quest_protocol import (
    CHAPTER_UNLOCKED_COMMAND,
    ChapterUnlocked,
)
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder
from dso_server.protocol.commerce_protocol import load_purchase_item_definition
from dso_server.protocol.item_receipt_protocol import (
    ITEM_RECEIVED_TEMPLATE_CATEGORIES,
    ItemReceiptGrant,
    build_item_receipts,
    item_received_message,
)
from dso_server.protocol.shop_protocol import OFFER_RARITY_IDS

from dso_server.services.base import CharacterService


ACHIEVEMENT_LOG_PREFIX = f"[{GAMEPLAY_SERVICE_NAME}] achievement:"


def _log(message: str) -> None:
    print(f"{ACHIEVEMENT_LOG_PREFIX} {message}")


def _achievement_state(s: Session) -> dict[str, AchievementProgress]:
    state = getattr(s, "gameplay_achievements", None)
    if state is None:
        state = {}
        s.gameplay_achievements = state
    return state


def _unlocked_chapters(s: Session) -> set[str]:
    chapters = getattr(s, "gameplay_chapters_unlocked", None)
    if chapters is None:
        chapters = set()
        s.gameplay_chapters_unlocked = chapters
    return chapters


class AchievementService:
    """Achievement logic mixed into ``CharacterServer``; state lives on the session."""

    # ------------------------------------------------------------------
    # Catalog and persistence
    # ------------------------------------------------------------------
    def _achievement_catalog(self) -> AchievementCatalog:
        return load_achievement_catalog(self.static_database_path)

    @staticmethod
    def _restore_achievement_state(s: Session, persisted_state) -> None:
        documents = getattr(persisted_state, "achievements", None) or {}
        s.gameplay_achievements = {
            str(key).casefold(): AchievementProgress.from_document(document)
            for key, document in documents.items()
            if isinstance(document, dict)
        }
        chapters = getattr(persisted_state, "chapters_unlocked", None) or ()
        s.gameplay_chapters_unlocked = {
            str(chapter).casefold() for chapter in chapters if str(chapter)
        }
        # The collected-skin set exists for the UnlockSkin rows; restored
        # here so its first reconcile extends, never replaces, the record.
        s.gameplay_unlocked_skins = {
            str(skin) for skin in getattr(persisted_state, "unlocked_skins", None) or ()
            if str(skin)
        }

    @staticmethod
    def _achievement_document(s: Session) -> dict[str, dict]:
        return {
            key: progress.to_document()
            for key, progress in sorted(_achievement_state(s).items())
            if progress.progress or progress.completed
        }

    @staticmethod
    def _chapters_unlocked_document(s: Session) -> tuple[str, ...]:
        return tuple(sorted(_unlocked_chapters(s)))

    # ------------------------------------------------------------------
    # Snapshots for the client
    # ------------------------------------------------------------------
    def _achievement_records(self, s: Session) -> tuple[Achievement, ...]:
        """Every achievement with progress, for the initialization snapshot."""

        catalog = self._achievement_catalog()
        records = []
        for key, progress in sorted(_achievement_state(s).items()):
            definition = catalog.achievement(key)
            if definition is None or not (progress.progress or progress.completed):
                continue
            records.append(Achievement(definition.achievement_id, progress.wire_values()))
        return tuple(records)

    def _achievement_init_info(self, s: Session) -> AchievementInfo:
        """Replay earned progress, preserving historical unlocks and stamps."""
        catalog = self._achievement_catalog()
        store = getattr(self, "account_store", None)
        with s.gameplay_mutation_lock, getattr(store, "achievement_lock", nullcontext()):
            shared = self._shared_achievement_snapshot(s, catalog)
            with s.flow_lock:
                state = _achievement_state(s)
                state.update(shared)
                changes = refresh_derived_achievements(catalog, state)
                self._mark_achievement_rewards(changes)
                s.gameplay_achievement_shared_dirty = bool(shared or changes)
            return AchievementInfo(self._achievement_records(s))

    def _shared_achievement_snapshot(self, s, catalog):
        store = getattr(self, "account_store", None)
        character = s.gameplay_character
        if store is None or character is None:
            return {}
        return {key: AchievementProgress.from_document(document)
                for key, document in store.achievement_snapshot(
                    character.character_id, catalog).items()}

    @staticmethod
    def _achievement_persist_lock(s):
        with s.flow_lock:
            lock = getattr(s, "gameplay_persist_lock", None)
            if lock is None:
                lock = threading.RLock()
                s.gameplay_persist_lock = lock
            return lock

    @staticmethod
    def _mark_achievement_rewards(changes):
        for change in changes:
            if (change.completed_now and change.definition.reward_item
                    and change.definition.reward_amount > 0):
                change.progress.reward_status = "pending"

    def _prepare_achievement_rewards(self, s, catalog):
        """Stage each pending item with its receipt; full bags remain pending.

        The caller holds the mutation and account achievement locks, persists
        the complete snapshot, then publishes these already encoded packets.
        """
        with s.flow_lock:
            pending = tuple(key for key, value in _achievement_state(s).items()
                            if value.completed and value.reward_status == "pending")
        packets = []
        for key in pending:
            definition = catalog.achievement(key)
            if definition is None or not definition.reward_item or definition.reward_amount <= 0:
                continue
            try:
                reward_item = definition.reward_item
                if "|" in reward_item:
                    class_name = CLASS_NAMES.get(int(getattr(s.gameplay_character, "class_id", 0)), "").casefold()
                    choices = dict(entry.split("|", 1) for entry in reward_item.split(";") if "|" in entry)
                    reward_item = choices.get(class_name, "")
                    if not reward_item:
                        raise ValueError("reward has no entry for this character class")
                item = load_purchase_item_definition(
                    self.static_database_path, reward_item)
                with s.flow_lock:
                    previous_inventory = s.gameplay_inventory
                    previous_id = s.gameplay_next_item_id
                    character = s.gameplay_character
                    if previous_inventory is None or character is None:
                        continue
                    builder = InventoryMutationBuilder(previous_inventory, previous_id)
                builder.add_item(item, definition.reward_amount,
                                 item_level=max(1, int(character.level)))
                mutation = builder.finish()
                receipts, _ = build_item_receipts(
                    previous_inventory, mutation.inventory,
                    (entry.item_id for entry in mutation.changed_items),
                    mutation.replacements,
                    (ItemReceiptGrant(
                        item.template_id, definition.reward_amount,
                        include_template_id=(
                            item.item_category in ITEM_RECEIVED_TEMPLATE_CATEGORIES
                        ),
                    ),),
                    next_item_id=mutation.next_item_id,
                )
                packet = encode_server_command_batch((
                    ServerCommand(INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID),
                    *(ServerCommand(MESSAGE_COMMAND, item_received_message(receipt),
                                    LOCAL_PLAYER_ACTOR_ID) for receipt in receipts),
                    ServerCommand(INVENTORY_INFO_COMMAND, mutation.final, LOCAL_PLAYER_ACTOR_ID),
                ), include_terminator=True)
            except (OSError, ValueError) as exc:
                _log(f"reward {key!r} pending: {exc}")
                continue
            with s.flow_lock:
                progress = _achievement_state(s)[key]
                if (s.gameplay_inventory is not previous_inventory
                        or s.gameplay_next_item_id != previous_id
                        or progress.reward_status != "pending"):
                    continue
                s.gameplay_inventory = mutation.inventory
                s.gameplay_next_item_id = mutation.next_item_id
                progress.reward_status = "granted"
            packets.append(packet)
        return tuple(packets)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    def _send_achievement_changes(self, s: Session, changes, *, label: str,
                                  deferred_packets=None) -> None:
        changes = tuple(changes)
        if not changes:
            return
        # One record per command, chained in a batch, as retail sends them.
        commands = tuple(
            ServerCommand(
                ACHIEVEMENT_INFO_COMMAND,
                AchievementInfo((Achievement(
                    change.definition.achievement_id, change.progress.wire_values(),
                ),)),
                LOCAL_PLAYER_ACTOR_ID,
            )
            for change in changes
        )
        packet, packet_bits = encode_server_command_batch(
            commands, include_terminator=True,
        )
        if deferred_packets is not None:
            deferred_packets.append((packet, packet_bits, label))
        else:
            CharacterService._send_world_loot_state_batch(s, packet, packet_bits, label=label)
        for change in changes:
            _log(
                f"{change.definition.achievement_id!r} "
                f"{change.progress.progress}/{change.definition.target}"
                + (" completed" if change.completed_now else "")
            )

    # ------------------------------------------------------------------
    # World events
    # ------------------------------------------------------------------
    def _achievement_feed(
        self, s: Session, kind: str, values, *, count: int = 1,
        progress: int | None = None,
        deferred_packets: list | None = None,
    ) -> tuple[AchievementChange, ...]:
        values = tuple(str(value) for value in values if str(value))
        try:
            catalog = self._achievement_catalog()
        except Exception as exc:
            _log(f"catalog unavailable: {type(exc).__name__}: {exc}")
            return ()
        store = getattr(self, "account_store", None)
        label = f"achievement-{kind}"
        with (s.gameplay_mutation_lock,
              getattr(store, "achievement_lock", nullcontext()),
              self._achievement_persist_lock(s)):
            shared = self._shared_achievement_snapshot(s, catalog)
            with s.flow_lock:
                state = _achievement_state(s)
                before_progress = copy.deepcopy(state)
                before_inventory = s.gameplay_inventory
                before_item_id = s.gameplay_next_item_id
                old_shared = {key: state[key].to_document() if key in state else None
                              for key in shared}
                state.update(shared)
                changes = apply_achievement_event(
                    catalog, state, kind, values, count=count, progress=progress,
                )
                derived = refresh_derived_achievements(catalog, state)
                changes = (*changes, *derived)
                self._mark_achievement_rewards(changes)
                shared_changed = any(old_shared[key] != value.to_document()
                                     for key, value in shared.items())
                shared_changed = shared_changed or getattr(s, "gameplay_achievement_shared_dirty", False)
            packets = self._prepare_achievement_rewards(s, catalog)
            if changes or packets or shared_changed:
                # Shared (account-wide) progress, completion receipts and item
                # grants reach SQLite in the same commit before any outbound
                # publication. A character's own progress alone (an ordinary
                # kill counter moving) publishes the same atomic snapshot to
                # the writer without waiting for the disk: it used to wait for
                # SQLite on every kill while holding this player's input gate.
                # Only 4 of 923 achievements are account-wide kill counters.
                durable = bool(packets) or shared_changed or any(
                    getattr(change, "completed_now", False) for change in changes
                )
                try:
                    saved = False
                    if store is not None and s.gameplay_inventory is not None:
                        saved = self._persist_gameplay_session(
                            s, reason=label, flush=durable, achievement_catalog=catalog)
                    if not saved:
                        CharacterService._request_account_save(s, label, flush=durable)
                except Exception:
                    # A flush error can occur after the writer accepted the
                    # atomic snapshot: retain that receipt and let it retry.
                    # An error before publication must roll back the staged
                    # grant, otherwise an older shared pending receipt could
                    # grant it a second time on the next event.
                    published = store.get(s.gameplay_character.character_id) if store is not None else None
                    if published is None or published.achievements != self._achievement_document(s):
                        with s.flow_lock:
                            s.gameplay_achievements = before_progress
                            s.gameplay_inventory = before_inventory
                            s.gameplay_next_item_id = before_item_id
                    s.gameplay_achievement_shared_dirty = True
                    raise
                s.gameplay_achievement_shared_dirty = False
                try:
                    for packet, packet_bits in packets:
                        if deferred_packets is not None:
                            deferred_packets.append((packet, packet_bits, "achievement-reward"))
                        else:
                            CharacterService._send_world_loot_state_batch(
                                s, packet, packet_bits, label="achievement-reward")
                    self._send_achievement_changes(s, changes, label=label,
                                                   deferred_packets=deferred_packets)
                except Exception as exc:
                    # Transport failure must not suppress the caller's other
                    # post-commit progression hooks. Relog replays the snapshot.
                    _log(f"publication failed after save: {type(exc).__name__}: {exc}")
            return tuple(changes)

    def _achievement_on_map_entered(self, s: Session, map_keys) -> None:
        """``map_keys``: the map id plus its access group / access id."""

        map_keys = tuple(dict.fromkeys(map_keys))
        self._achievement_feed(s, "enter_map", map_keys)
        character = s.gameplay_character
        if character is not None:
            self._achievement_on_class_level(s, character.class_id, character.level)
        self._achievement_on_equipment_changed(s)
        self._achievement_on_stickerbook_changed(s)
        # Login/map entry reconciles the snapshot rules: items gained through
        # shops, chests or rewards since the last hook, and the account's age.
        self._achievement_on_skins_collected(s, reconcile=True)
        self._achievement_on_veteran_status(s)
        for map_key in map_keys:
            self._season_pass_feed(s, "EnterMap", value=str(map_key))

    @staticmethod
    def _achievement_kill_difficulty(s: Session) -> int:
        """Difficulty id of the session's map, as daily challenges read it."""

        try:
            return max(0, int(getattr(s, "gameplay_map_difficulty", 0) or 0),
                       int(getattr(s, "gameplay_scaling_difficulty", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _achievement_on_monsters_killed(self, s: Session, monsters) -> None:
        """Queue kills for achievements and daily challenges, applied in batches.

        Each application takes the player's mutation gate (which their casts
        and moves hold too), deep-copies the achievement state, re-derives
        derived achievements and, since kill counters change on every kill,
        saves synchronously. Done per kill, area fights spent most of the
        hit workers' time here and casts queued behind it. Kills within
        KILL_PROGRESS_BATCH_SECONDS are applied together instead.
        """

        entries = tuple(
            (monster, self._achievement_kill_difficulty(s)) for monster in monsters
        )
        if not entries:
            return
        # Composed test servers never call start() and apply kills at once.
        if KILL_PROGRESS_BATCH_SECONDS <= 0 or not getattr(
                self, "_kill_progress_batching", False):
            self._apply_monster_kill_progress(s, entries)
            return
        with s.flow_lock:
            s.gameplay_pending_kill_progress.extend(entries)
            schedule = not s.gameplay_kill_progress_flush_scheduled
            s.gameplay_kill_progress_flush_scheduled = True
        if not schedule:
            return
        try:
            timer = s.gameplay_deadline_timer(
                KILL_PROGRESS_BATCH_SECONDS,
                CharacterService._queue_monster_kill_progress_flush,
                args=(self, s),
            )
            timer.daemon = True
            timer.start()
        except Exception as exc:
            _log(f"kill progress timer unavailable: {type(exc).__name__}: {exc}")
            CharacterService._flush_monster_kill_progress(self, s)

    def _queue_monster_kill_progress_flush(self, s: Session) -> None:
        # The timer runs on the hit/impact pool; the work itself belongs on
        # the progression queue with the rest of the kill's consequences.
        if not progression_queue.defer(
            CharacterService._flush_monster_kill_progress, self, s,
            label="kill progress",
        ):
            CharacterService._flush_monster_kill_progress(self, s)

    def _flush_monster_kill_progress(self, s: Session) -> None:
        with s.flow_lock:
            entries = tuple(s.gameplay_pending_kill_progress)
            s.gameplay_pending_kill_progress.clear()
            s.gameplay_kill_progress_flush_scheduled = False
        if entries and s.gameplay_character is not None:
            try:
                self._apply_monster_kill_progress(s, entries)
            except Exception as exc:
                _log(f"kill progress for {len(entries)} kill(s) failed: "
                     f"{type(exc).__name__}: {exc}")
                traceback.print_exc()

    def _apply_monster_kill_progress(self, s: Session, entries) -> None:
        """Apply (monster, map difficulty at the kill) pairs in one pass."""

        monsters = tuple(monster for monster, _difficulty in entries)
        self._daily_challenge_monsters_killed(s, monsters)
        counts: dict[tuple[str, int], int] = {}
        for monster, difficulty in entries:
            template = str(getattr(monster, "template_id", ""))
            if template:
                key = (template, int(difficulty))
                counts[key] = counts.get(key, 0) + 1
        # Kill rows name either the template or "<template>:<difficulty>"
        # (pw_difficulty_08_defeatBoss "boss_grimmag:5"); "kill" is a one-hit
        # kind, so a row naming both cannot count one kill twice.
        for (template, difficulty), count in counts.items():
            self._achievement_feed(
                s, "kill", (template, f"{template}:{difficulty}"), count=count,
            )
        with s.flow_lock:
            inventory = s.gameplay_inventory
            character = s.gameplay_character
            class_name = CLASS_NAMES.get(int(getattr(character, "class_id", 0)), "").lower()
            equipped = tuple(f"{class_name}:{item.template_id}"
                             for item in inventory.items
                             if item.item_id in inventory.equipment_slots) if inventory else ()
        if counts and equipped:
            self._achievement_feed(s, "itemkill", equipped, count=sum(counts.values()))

    def _achievement_on_equipment_changed(self, s: Session) -> None:
        with s.flow_lock:
            inventory = s.gameplay_inventory
            items = tuple(item for item in inventory.items
                          if item.item_id in inventory.equipment_slots) if inventory else ()
            templates = tuple(item.template_id for item in items)
            rarities = {value: key for key, value in OFFER_RARITY_IDS.items()}
            equipped_rarities = tuple(rarities.get(item.field_08, "") for item in items)
        self._achievement_feed(s, "equipitem", templates)
        self._achievement_feed(s, "equiprarity", equipped_rarities)
        self._achievement_on_skins_collected(s)

    def _achievement_on_skins_collected(self, s: Session, *, reconcile: bool = False) -> None:
        """Add the looks of held items to the collection; feed its size.

        UnlockSkin counts skins collected from equipment the character has
        held (gameplay/skin_collection.py), as an absolute snapshot of the
        persisted set: the same item, a re-equip or a relog adds nothing.
        Hooks run it on equipment changes and pickups; ``reconcile`` (map
        entry) also feeds an unchanged set, which migrates older characters
        and items gained through shops, chests or rewards in between.
        """

        with s.flow_lock:
            inventory = s.gameplay_inventory
            character = s.gameplay_character
            collected = getattr(s, "gameplay_unlocked_skins", None)
            items = tuple(inventory.items) if inventory is not None else ()
        if inventory is None or character is None or collected is None:
            return
        class_name = CLASS_NAMES.get(int(getattr(character, "class_id", 0)), "")
        try:
            catalog = load_skin_catalog(self.static_database_path)
        except (OSError, sqlite3.Error) as exc:
            _log(f"skin catalog unavailable: {type(exc).__name__}: {exc}")
            return
        held = held_skins(catalog, items, class_name)
        with s.flow_lock:
            current = set(getattr(s, "gameplay_unlocked_skins", None) or ())
            added = held - current
            if added:
                # Replace rather than mutate: persistence snapshots the set.
                s.gameplay_unlocked_skins = current | added
            total = len(current | added)
        if not total or (not added and not reconcile):
            return
        changes = self._achievement_feed(s, "unlockskin", (), progress=total)
        if added:
            _log(f"collected {len(added)} skin(s), {total} in total")
            if not changes:
                CharacterService._request_account_save(s, "skins collected")

    def _achievement_on_veteran_status(self, s: Session) -> None:
        """Feed the account's veteran rank (``_Template_VeteranLevel``).

        Needs the account registry: account creation time and the levels of
        every character it owns (gameplay/veteran_levels.py).
        """

        character = s.gameplay_character
        store = getattr(self, "account_store", None)
        registry = getattr(self, "account_registry", None) or getattr(
            store, "account_registry", None)
        if character is None or registry is None:
            return
        character_id = int(character.character_id)
        try:
            owner = registry.character_owner(character_id)
            if owner is None:
                return
            levels = [int(character.level)]
            for other_id in owner.character_ids:
                if int(other_id) == character_id or store is None:
                    continue
                profile = store.get_profile(int(other_id))
                if profile is not None:
                    levels.append(int(profile.level))
            rank = veteran_level(
                load_veteran_rules(self.static_database_path),
                account_created_at=float(owner.created_at),
                character_levels=levels,
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            _log(f"veteran rank unavailable: {type(exc).__name__}: {exc}")
            return
        if rank is not None:
            self._achievement_feed(s, "veteranlevelunlock", (rank.level_id,))

    def _achievement_on_prop_interacted(self, s: Session, instance_id: str, template_id: str) -> None:
        if any(token in (instance_id + ' ' + template_id).casefold()
               for token in ('cooking_chest_', 'cooking_collect_meat_chest')):
            self._daily_challenge_feed(s, 'PropInteractionCookingBox')
        self._achievement_feed(s, "interact", (instance_id, template_id))

    def _achievement_on_item_collected(self, s: Session, template_id: str, amount: int = 1) -> None:
        self._daily_challenge_feed(s, "ItemCollected", value=template_id, count=amount)
        self._achievement_feed(s, "pick_up", (template_id,), count=max(1, int(amount)))
        self._achievement_on_skins_collected(s)

    def _achievement_on_quest_closed(self, s: Session, quest_id: str) -> None:
        operations = [lambda: self._daily_challenge_feed(s, "QuestsClosed")]
        wanted = self._wanted_catalog()
        if any(mission.quest_id.casefold() == str(quest_id).casefold()
               for mission in wanted.missions.values()):
            operations.append(lambda: self._season_pass_feed(s, "WantedThief"))
        operations.append(lambda: self._achievement_feed(s, "quest", (quest_id,)))
        failure = None
        for operation in operations:
            try:
                operation()
            except OSError as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    def _achievement_on_dungeon_cleared(self, s: Session, map_id: str) -> None:
        self._achievement_feed(s, "cleardungeon", (map_id,))

    @staticmethod
    def _achievement_on_level_up(s: Session, class_id: int, level: int) -> None:
        """Raise a level change from static progression code."""

        callback = getattr(s, "gameplay_presence_callback", None)
        if callback is None:
            return
        event = ("level_up", int(class_id), int(level))
        # Level-up signals, quest introductions and achievements follow the
        # kill that caused them on the progression queue.
        if progression_queue.defer(
            AchievementService._raise_level_up, s, event, label="level up",
            guard=progression_queue.same_character(s),
        ):
            return
        AchievementService._raise_level_up(s, event)

    @staticmethod
    def _raise_level_up(s: Session, event) -> None:
        callback = getattr(s, "gameplay_presence_callback", None)
        if callback is None:
            return
        try:
            callback(s, event)
        except Exception as exc:
            _log(f"level notification failed: {type(exc).__name__}: {exc}")

    def _achievement_on_class_level(self, s: Session, class_id: int, level: int) -> None:
        class_name = CLASS_NAMES.get(int(class_id), "")
        if not class_name:
            return
        self._achievement_feed(s, "class_level", (class_name, str(int(level))))

    # ------------------------------------------------------------------
    # Chapters
    # ------------------------------------------------------------------
    def _chapter_unlock_commands(self, s: Session, chapter_id: str) -> tuple[ServerCommand, ...]:
        """``ChapterUnlocked`` for ``chapter_id`` the first time it is seen."""

        key = str(chapter_id or "").casefold()
        if not key or not announced_chapter(key):
            return ()
        with s.flow_lock:
            chapters = _unlocked_chapters(s)
            if key in chapters:
                return ()
            chapters.add(key)
        _log(f"chapter {chapter_id!r} unlocked")
        return (ServerCommand(
            CHAPTER_UNLOCKED_COMMAND, ChapterUnlocked(str(chapter_id)),
            LOCAL_PLAYER_ACTOR_ID,
        ),)


__all__ = ["AchievementService"]
