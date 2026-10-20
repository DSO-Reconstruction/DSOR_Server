"""Achievement and chapter progression, persisted per character.

Retail reports every progress change with one
``Achievements::AchievementInfoCommand`` record and the client raises its
own popup when a record carries a completion timestamp
(docs/QUEST_PROTOCOL.md §11).  Both the progress counters and the
completion stamps live in ``AccountState.achievements`` so a later login
replays the same records (same timestamps, no fresh popups), and
``AccountState.chapters_unlocked`` remembers which ``ChapterUnlocked``
notifications the character already received.

World happenings reach the service through the same hooks the quest
service uses: ``_on_map_entered`` (Enter_Map), ``_on_monsters_killed``
(Kill), ``_handle_prop_interaction`` (Interact), the presence callback's
``item_collected`` (Pick_Up), ``_quest_turn_in`` (Quest) and
``_award_experience`` (Class_Level).  Points and UnlockAchievements rows
are derived from the completed set after every completion.
"""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.gameplay.achievement_content import (
    AchievementCatalog,
    AchievementChange,
    AchievementProgress,
    apply_achievement_event,
    complete_achievement,
    load_achievement_catalog,
    refresh_derived_achievements,
)
from dso_server.gameplay.quest_content import announced_chapter
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.player_init_protocol import (
    ACHIEVEMENT_INFO_COMMAND,
    Achievement,
    AchievementInfo,
    ServerCommand,
    encode_server_command_batch,
)
from dso_server.protocol.quest_protocol import (
    CHAPTER_UNLOCKED_COMMAND,
    ChapterUnlocked,
)

from dso_server.services.base import CharacterService


ACHIEVEMENT_LOG_PREFIX = f"[{GAMEPLAY_SERVICE_NAME}] achievement:"
RUNE_TRINKET_SLOT_UNLOCK_ACHIEVEMENTS = (
    "item_use_item_rune_extension_stone",
    "item_use_item_rune_extension_stone_02",
)


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
        """The AchievementInfo aggregate of a map initialization.

        The recipe-gate achievements the local server hands out (the
        crafting recipes are unlocked by them) are completed here once; the
        stamp is then persisted, so a relog replays the same record instead
        of a fresh unlock.
        """

        catalog = self._achievement_catalog()
        state = _achievement_state(s)
        granted = []
        for resource_id, _required in load_recipe_unlock_achievements(
            self.static_database_path
        ):
            definition = catalog.achievement(resource_id)
            if definition is None:
                continue
            if complete_achievement(state, definition):
                granted.append(definition.achievement_id)
        for resource_id in RUNE_TRINKET_SLOT_UNLOCK_ACHIEVEMENTS:
            definition = catalog.achievement(resource_id)
            if definition is not None and complete_achievement(
                state, definition
            ):
                granted.append(definition.achievement_id)
        if granted:
            refresh_derived_achievements(catalog, state)
            _log(f"granted initialization achievements {granted!r}")
            CharacterService._request_account_save(
                s, "initialization achievements granted"
            )
        return AchievementInfo(self._achievement_records(s))

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    def _send_achievement_changes(self, s: Session, changes, *, label: str) -> None:
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
        CharacterService._send_world_loot_state_batch(s, packet, packet_bits, label=label)
        for change in changes:
            _log(
                f"{change.definition.achievement_id!r} "
                f"{change.progress.progress}/{change.definition.target}"
                + (" completed" if change.completed_now else "")
            )
        CharacterService._request_account_save(s, label)

    # ------------------------------------------------------------------
    # World events
    # ------------------------------------------------------------------
    def _achievement_feed(
        self, s: Session, kind: str, values, *, count: int = 1,
    ) -> tuple[AchievementChange, ...]:
        values = tuple(str(value) for value in values if str(value))
        if not values:
            return ()
        try:
            catalog = self._achievement_catalog()
        except Exception as exc:
            _log(f"catalog unavailable: {type(exc).__name__}: {exc}")
            return ()
        with s.flow_lock:
            changes = apply_achievement_event(
                catalog, _achievement_state(s), kind, values, count=count,
            )
        if changes:
            self._send_achievement_changes(s, changes, label=f"achievement-{kind}")
        return changes

    def _achievement_on_map_entered(self, s: Session, map_keys) -> None:
        """``map_keys``: the map id plus its access group / access id."""

        map_keys = tuple(dict.fromkeys(map_keys))
        self._achievement_feed(s, "enter_map", map_keys)
        for map_key in map_keys:
            self._season_pass_feed(s, "EnterMap", value=str(map_key))

    def _achievement_on_monsters_killed(self, s: Session, monsters) -> None:
        self._daily_challenge_monsters_killed(s, monsters)
        counts: dict[str, int] = {}
        for monster in monsters:
            template = str(getattr(monster, "template_id", ""))
            if template:
                counts[template] = counts.get(template, 0) + 1
        for template, count in counts.items():
            self._achievement_feed(s, "kill", (template,), count=count)

    def _achievement_on_prop_interacted(self, s: Session, instance_id: str, template_id: str) -> None:
        if any(token in (instance_id + ' ' + template_id).casefold()
               for token in ('cooking_chest_', 'cooking_collect_meat_chest')):
            self._daily_challenge_feed(s, 'PropInteractionCookingBox')
        self._achievement_feed(s, "interact", (instance_id, template_id))

    def _achievement_on_item_collected(self, s: Session, template_id: str, amount: int = 1) -> None:
        self._daily_challenge_feed(s, "ItemCollected", value=template_id, count=amount)
        self._achievement_feed(s, "pick_up", (template_id,), count=max(1, int(amount)))

    def _achievement_on_quest_closed(self, s: Session, quest_id: str) -> None:
        self._daily_challenge_feed(s, "QuestsClosed")
        wanted = self._wanted_catalog()
        if any(mission.quest_id.casefold() == str(quest_id).casefold()
               for mission in wanted.missions.values()):
            self._season_pass_feed(s, "WantedThief")
        self._achievement_feed(s, "quest", (quest_id,))

    def _achievement_on_dungeon_cleared(self, s: Session, map_id: str) -> None:
        self._achievement_feed(s, "cleardungeon", (map_id,))

    @staticmethod
    def _achievement_on_level_up(s: Session, class_id: int, level: int) -> None:
        """Raise a level change from static progression code."""

        callback = getattr(s, "gameplay_presence_callback", None)
        if callback is None:
            return
        try:
            callback(s, ("level_up", int(class_id), int(level)))
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
