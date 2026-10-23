"""Quest service: dialogs, acceptance, task progress, turn-in, tracking.

The wire behaviour follows the retail tutorial capture documented in
docs/QUEST_PROTOCOL.md; the task rules come from ``data/quests.xml`` through
``gameplay/quest_content.py``.  World happenings reach the service through
small hooks in the existing services: ``_handle_npc_request`` (encounters
and dialog offers), ``_on_monsters_killed`` (kills, cleared regions),
``_pickup_world_item`` (collects), ``_handle_prop_interaction`` (interacts),
``_check_trigger_volumes`` / ``_handle_quest_trigger_signal`` (explore
triggers) and ``_emit_map_signal`` (self-starting quests).  Quest state in
turn feeds the signal engine (``QuestAccepted``/``QuestSolved``/
``QuestTaskDone`` conditions and the ``PlayerOpenedQuest`` /
``PlayerEndQuestTask`` / ``PlayerClosedQuest`` events).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
import random

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.gameplay.local_map_state import (
    LOCAL_NPC_ACTOR_BASE,
    load_local_event_set_flags,
    load_local_npc_definitions,
    local_npc_actor_id,
    local_npc_info,
)
from dso_server.gameplay.map_event_state import (
    PARALLEL_WORLD_REWARD_CHEST_INSTANCE_IDS,
    load_map_signal_catalog,
    map_key,
    prop_visible_to_player,
)
from dso_server.gameplay.quest_content import (
    MAX_TASKS,
    NO_TIMESTAMP,
    QUEST_STATE_ACCEPTED,
    QUEST_STATE_CLOSED,
    QUEST_STATE_CANCELLED,
    QUEST_STATE_FINISHED,
    Drop,
    QuestCatalog,
    QuestDefinition,
    QuestProgress,
    QuestSpawn,
    active_prop_switches,
    active_tasks,
    apply_quest_event,
    explore_trigger_for_task,
    load_quest_catalog,
    task_counters,
)
from dso_server.paths import DEFAULT_QUESTS_PATH
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.gameplay_protocol import (
    ActorsEnterVicinity,
    ActorsEnterVicinityBatch,
    ActorPlacement,
    NPC_INFO_COMMAND,
    NPCBatch,
    NPCFlag,
    NPCInfo,
    NPCInteraction,
    NPCStringSet,
    NewNPC,
    encode_actors_enter_vicinity_batch,
    encode_npc_batch,
)
from dso_server.protocol.loot_protocol import _credit_currency
from dso_server.protocol.prop_protocol import encode_discard_prop_batch
from dso_server.protocol.player_init_protocol import (
    CURRENCY_CHANGED_COMMAND,
    ChapterProgress,
    QuestLogInfo,
    ServerCommand,
    encode_event_update,
    encode_server_command_batch,
)
from dso_server.protocol.quest_protocol import (
    CHAPTER_PROGRESS_COMMAND,
    NPC_QUEST_DIALOG,
    NPC_TALK_DIALOG,
    NPC_TASK_TARGET,
    QUEST_COMMAND,
    DELIVER_COMMAND,
    NPC_DELIVERY_DIALOG,
    decode_deliver,
    QUEST_MONSTER_INDICATION_INFO_COMMAND,
    QUEST_MONSTER_INDICATION_REQUEST_COMMAND,
    QUEST_OPERATION_ACCEPT,
    QUEST_OPERATION_CANCEL,
    QUEST_OPERATION_TURN_IN,
    QUEST_TRIGGER_INDICATION_INFO_COMMAND,
    QUEST_TRIGGER_INDICATION_REQUEST_COMMAND,
    QUEST_UPDATE_COMMAND,
    TALK_COMMAND,
    QuestMonsterIndication,
    QuestMonsterIndicationInfo,
    QuestTriggerIndication,
    QuestTriggerIndicationInfo,
    QuestUpdate,
    chapter_progress,
    decode_quest_command,
    decode_quest_indication_request,
    decode_talk,
    quest_log_entry,
)
from dso_server.protocol.prop_protocol import (
    PROP_STATE_ACTIVE,
    PROP_STATE_USED,
)
from dso_server.protocol.reward_protocol import RewardDefinition
from dso_server.protocol.world_loot_protocol import (
    build_quest_world_drops, quest_item_template_id,
)
from dso_server.protocol.inventory_codec import owned_inventory_items
from dso_server.protocol.commerce_protocol import load_purchase_item_definition

from dso_server.services.base import CharacterService
from dso_server.services.player_progression import load_player_progression_catalog
from dso_server.gameplay.status_modifier_runtime import (
    evaluate_active_status_modifiers, load_status_modifier_catalog, snapshot_active_status_entries,
)
from dso_server.services.map_events import (
    _PARALLEL_WORLD_BOSS_REWARD_CHESTS,
    _send_prop_info_entries,
)


QUEST_LOG_PREFIX = f"[{GAMEPLAY_SERVICE_NAME}] quest:"


def _log(message: str) -> None:
    print(f"{QUEST_LOG_PREFIX} {message}")


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _quest_log(s: Session) -> dict[str, QuestProgress]:
    log = getattr(s, "gameplay_quest_log", None)
    if log is None:
        log = {}
        s.gameplay_quest_log = log
    return log


def _quest_item_templates(quest: QuestDefinition) -> set[str]:
    """All authored task items, including completed and future objectives."""
    items = set()
    for task in quest.tasks:
        for event in task.events:
            items.update(drop.item_id for drop in event.drops)
            if event.kind == "collect":
                items.add(event.value)
        items.update(item for barter in task.barters for item in barter.items)
        items.update(switch.item for switch in task.switch_definitions if switch.item)
    return {quest_item_template_id(item).casefold() for item in items}


def _character_facts(s: Session) -> tuple[str, int]:
    character = getattr(s, "gameplay_character", None)
    class_name = CLASS_NAMES.get(int(getattr(character, "class_id", -1)), "")
    level = max(1, int(getattr(character, "level", 1) or 1))
    return class_name, level


def _act_of(chapter_id: str) -> str:
    key = str(chapter_id)
    marker = key.find("_chapter")
    return key[:marker] if marker > 0 else key


class QuestService:
    """Quest logic mixed into ``CharacterServer``; state lives on the session."""

    # ------------------------------------------------------------------
    # Catalog and persistence
    # ------------------------------------------------------------------
    def _quest_catalog(self) -> QuestCatalog:
        path = getattr(self, "quests_path", None) or DEFAULT_QUESTS_PATH
        return load_quest_catalog(path)

    def _quest_active_events(self, s: Session) -> frozenset[str]:
        # Read the schedule now, not the map-entry snapshot: event dates or
        # operator overrides may have changed while the character was online.
        active = self._event_catalog().active_event_flags()
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is not None:
            with runtime.lock:
                # initial_flags also contains scheduled events captured on
                # entry. Only running local events may supplement the schedule.
                active |= runtime.running_events()
        return active

    def _quest_reveal_flags(self, s: Session, map_id: str) -> frozenset[str]:
        """Declare the event sets active and finished quest targets hide behind.

        A map-local event reveals its EventSet content only in the instance
        that ran it, so a character that relogs after the reveal but before
        the meeting finds nobody standing there and can never finish the
        task. The quest log is the durable record of how far the player got,
        so open encounter/talk steps, finished quest goal NPCs, and givers for
        currently available quests re-activate the flags their target NPC is
        authored behind.
        """

        wanted = {str(map_id).casefold()}
        catalog = self._quest_catalog()
        targets = set()
        log = _quest_log(s)
        for quest_id, progress in log.items():
            quest = catalog.quest(quest_id)
            if quest is None:
                continue
            if progress.state == QUEST_STATE_FINISHED and quest.goal:
                # Keep a finished quest's turn-in NPC visible after a relog.
                targets.add(quest.goal.casefold())
            for task in active_tasks(quest, progress):
                if task.maps and not task.maps & wanted:
                    continue
                if (
                    task.task_type.casefold() == "completetask"
                    and quest.goal
                    and task.is_done(progress.tokens)
                ):
                    # Eventless CompleteTask objectives are completed by
                    # encountering the goal NPC. Keep that actor's EventSet
                    # visible after the preceding delivery/task finishes.
                    targets.add(quest.goal.casefold())
                # A delivery task has no encounter/talk event to keep its
                # recipient's EventSet visible. Preserve that NPC while the
                # hand-in is active, including after a map re-entry.
                targets.update(
                    barter.actor.casefold()
                    for barter in task.barters
                    if barter.actor and barter.live(progress.tokens)
                )
                for event in task.events:
                    if not event.value:
                        continue
                    if event.kind == "encounter":
                        targets.add(event.value.casefold())
                    elif event.kind == "talk":
                        # Talk events name the dialog, not the NPC; the
                        # task's dialog table maps it to the meeting target.
                        actor = task.dialogs.get(event.value)
                        if actor:
                            targets.add(actor.casefold())
                        else:
                            # Fall back to quest-wide dialog lookup in case
                            # the event value is the bare dialog short name.
                            for dialog_id, dialog_actor in task.dialogs.items():
                                if dialog_id.casefold().endswith(
                                    event.value.casefold()
                                ):
                                    targets.add(dialog_actor.casefold())
                                    break
        # Some quest givers are themselves hidden behind an EventSet. Once a
        # prerequisite is turned in, the next quest becomes available, but
        # there may be no active quest left whose encounter target reveals the
        # giver. Reveal eligible givers using the same availability rules used
        # when composing NPC quest offers.
        class_name, level = _character_facts(s)
        closed = [
            quest_id for quest_id, progress in log.items()
            if progress.state == QUEST_STATE_CLOSED
        ]
        active_events = self._quest_active_events(s)
        try:
            npc_definitions = load_local_npc_definitions(
                self.static_database_path,
                map_id,
                include_inactive_event_sets=True,
            )
        except Exception as exc:
            _log(
                f"quest NPC reveal lookup failed on {map_id}: "
                f"{type(exc).__name__}: {exc}"
            )
            npc_definitions = ()
        gated = {
            definition.template_id.casefold(): definition.event_set_row
            for definition in npc_definitions
            if definition.event_set_row >= 0
        }
        try:
            giver_sets = load_local_event_set_flags(
                self.static_database_path, map_id,
            )
            signal_catalog = load_map_signal_catalog(self.static_database_path)
        except Exception:
            giver_sets, signal_catalog = {}, None
        for npc_template_id, event_set_row in gated.items():
            # Seasonal givers (Halloween, New Moon, Grima's merchant set) sit
            # behind global event flags, not client-only map events; their
            # quests carry no EventId, so availability alone would show them
            # all year. Only reveal sets a client-only map event gates.
            if signal_catalog is None or not any(
                (event := signal_catalog.event(flag)) is not None
                and event.client_only
                for flag in giver_sets.get(event_set_row, ())
            ):
                continue
            if any(catalog.available(
                quest,
                class_name=class_name,
                level=level,
                closed_quests=closed,
                known_quests=log.keys(),
                active_events=active_events,
            ) for quest in catalog.offered_by(npc_template_id)):
                targets.add(npc_template_id)
        flags = set()
        if targets:
            declarations = load_local_event_set_flags(
                self.static_database_path, map_id,
            )
            flags.update({
            flag
            for target in targets
            for row in (gated.get(target),)
            if row is not None and row >= 0
            for flag in declarations.get(row, frozenset())
            })

        if str(map_id).casefold() == "f4102_dun":
            railroad_repair_complete = any(
                quest_id.casefold() == "f4100-railroadbarrier-02-n"
                and progress.state == QUEST_STATE_CLOSED
                for quest_id, progress in _quest_log(s).items()
            )
            with s.flow_lock:
                destination_discovered = map_key("f4201_pve") in {
                    map_key(value)
                    for value in getattr(s, "gameplay_unlocked_maps", ())
                }
                inventory = getattr(s, "gameplay_inventory", None)
                has_gate_key = bool(
                    inventory is not None
                    and any(
                        str(item.template_id).casefold()
                        == "key_from_f4102_to_f4201"
                        for item in inventory.items
                    )
                )
            if railroad_repair_complete or destination_discovered or has_gate_key:
                # This authored scene flag starts the repaired-train state and
                # keeps the Yildirimdbi gate available after its quest key is
                # consumed or the character relogs.
                flags.add("f4102_train_repair")

        if str(map_id).casefold() == "g0005_temple_ice_01_dun":
            with s.flow_lock:
                temple_discovered = map_key("g0005_temple_ice_02_dun") in {
                    map_key(value)
                    for value in getattr(s, "gameplay_unlocked_maps", ())
                }
            if temple_discovered:
                # The Temple gate's EventSet is opened by a signal that only
                # fires while quest 06 is accepted. Once the map is unlocked,
                # preserve that route flag on later visits and quest stages.
                flags.add("g0005_TempleOfIce_05_openExit")
        return frozenset(flags)

    @staticmethod
    def _restore_quest_state(s: Session, persisted_state) -> None:
        documents = getattr(persisted_state, "quest_log", None) or {}
        s.gameplay_quest_log = {
            str(quest_id): QuestProgress.from_document(quest_id, document)
            for quest_id, document in documents.items()
            if isinstance(document, dict)
        }
        s.gameplay_quest_tracking = None

    @staticmethod
    def _quest_log_document(s: Session) -> dict[str, dict]:
        return {
            quest_id: progress.to_document()
            for quest_id, progress in _quest_log(s).items()
        }

    def _quest_prepare_map(self, s: Session) -> None:
        """Discard stale dungeon runs before sending the new map's quest log."""
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        instance = getattr(runtime, "instance_id", "")
        map_id = str(s.gameplay_map or "").casefold()
        for quest_id, progress in tuple(_quest_log(s).items()):
            quest = self._quest_catalog().quest(quest_id)
            if quest is None or not quest.bound_to_map or progress.state == QUEST_STATE_CLOSED:
                continue
            if progress.continue_on_map == map_id and instance:
                progress.map_instance = instance
                progress.continue_on_map = ""
            elif not instance or progress.map_instance != instance:
                del _quest_log(s)[quest_id]

    def _quest_leave_map(self, s: Session, destination: str = "", *, notify: bool = True) -> None:
        """Cancel this run, preserving explicit onward stages of multi-map quests."""
        destination = str(destination).casefold()
        for quest_id, progress in tuple(_quest_log(s).items()):
            quest = self._quest_catalog().quest(quest_id)
            if quest is None or not quest.bound_to_map or progress.state == QUEST_STATE_CLOSED:
                continue
            # Single-map stages describe a route (e.g. PW dungeon -> dungeon ->
            # boss). A list of alternatives on one task describes independent
            # runs, such as Bloodshed, and must never carry progress between them.
            next_maps = {next(iter(task.maps)) for task in active_tasks(quest, progress)
                         if len(task.maps) == 1}
            if destination and destination != str(s.gameplay_map).casefold() and destination in next_maps:
                progress.continue_on_map = destination
            else:
                self._quest_cancel(s, quest_id, force=True, notify=notify)

    @staticmethod
    def _quest_run_key(s: Session, quest: QuestDefinition) -> tuple[int, str]:
        return (int(getattr(s.gameplay_character, "character_id", 0)), quest.key)

    def _quest_entry_signal_matches(self, s: Session, rule) -> bool:
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return False
        class_name, level = _character_facts(s)
        accepted, solved, tasks = self._quest_signal_facts(s)
        with runtime.lock:
            running = runtime.active_flags()
        params = {str(value).casefold() for value in
                  (s.gameplay_map, s.gameplay_map_access_id, s.gameplay_map_access_group_id) if value}
        return (not rule.params or bool(params.intersection(rule.params))) and rule.accepts(
            map_id=runtime.map_id, running_events=running, player_level=level,
            class_name=class_name, player_count=max(1, len(self._map_event_sessions(runtime))),
            quests_accepted=accepted, quests_solved=solved, tasks_done=tasks,
            unlocked_maps=frozenset(s.gameplay_unlocked_maps))

    def _quest_on_map_entered(self, s: Session) -> None:
        self._quest_recover_myrdosch_access_key(s)
        self._quest_recover_artaya_sanctuary_key(s)
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return
        # A map's initial EventUpdate may arrive before its local event
        # controller is ready. Re-send quest-derived activations after map
        # entry so gated client-only NPCs become interactable at every
        # access difficulty.
        try:
            if self._quest_reveal_flags(s, runtime.map_id):
                self._quest_refresh_event_state(s)
                self._quest_spawn_revealed_npcs(s)
        except Exception as exc:
            _log(
                f"quest event refresh after map entry failed: "
                f"{type(exc).__name__}: {exc}"
            )
        # These entry signals are authored as client reports. The server knows
        # the same facts and can start the quest even if that report is lost.
        for quest in self._quest_catalog().quests.values():
            if not quest.bound_to_map or not quest.auto_start:
                continue
            rule = runtime.catalog.rule(quest.start_signal)
            if rule is not None and rule.event_type in {"playerenteredmap", "playerenteredmapid"}:
                if self._quest_entry_signal_matches(s, rule):
                    self._quest_on_signal(s, rule.signal_id)
        self._quest_ensure_cult_shaman(s)
        self._quest_ensure_ice_guardian(s)
        self._quest_ensure_lantern_scout(s)
        # Clear-region objectives drive the client's remaining-monster
        # counter. Retail normally asks for this tracker from the UI, but
        # map-bound challenges can start automatically without that request.
        # Select the active objective on this map and publish its initial
        # monster set so the counter is visible immediately on entry.
        map_keys = {
            str(value).casefold()
            for value in (
                s.gameplay_map,
                getattr(s, "gameplay_map_access_id", ""),
                getattr(s, "gameplay_map_access_group_id", ""),
            )
            if value
        }
        catalog = self._quest_catalog()
        tracking = getattr(s, "gameplay_quest_tracking", None)
        tracking_is_current = False
        if tracking:
            tracked_id, tracked_task_index = tracking
            tracked_progress = next((progress for quest_id, progress in _quest_log(s).items()
                                     if quest_id.casefold() == str(tracked_id).casefold()), None)
            tracked_quest = catalog.quest(tracked_id)
            if tracked_progress is not None and tracked_quest is not None:
                tracking_is_current = any(
                    task.index == int(tracked_task_index)
                    and (not task.maps or bool(map_keys.intersection(
                        value.casefold() for value in task.maps
                    )))
                    for task in active_tasks(tracked_quest, tracked_progress)
                )
        # A stale or unrelated tracker must not hide the current dungeon's
        # map-bound clear-region objective. Keep an active objective that
        # really belongs to this map, but replace tracking left over from a
        # previous map or a quest whose task has already advanced.
        if not tracking_is_current:
            s.gameplay_quest_tracking = None
            for quest_id, progress in _quest_log(s).items():
                quest = catalog.quest(quest_id)
                if quest is None or not quest.bound_to_map:
                    continue
                task = next((task for task in active_tasks(quest, progress)
                             if task.clear_regions
                             and (not task.maps or bool(
                                 map_keys.intersection(
                                     value.casefold() for value in task.maps
                                 )
                             ))), None)
                if task is not None:
                    s.gameplay_quest_tracking = (quest.quest_id, task.index)
                    self._quest_send_monster_indication(s)
                    break
        # Shared dungeons may already be cleared when another player joins.
        # Reconcile against the actual population, never an empty spawn table.
        self._quest_check_cleared_regions(s)

    def _quest_ensure_cult_shaman(self, s: Session) -> None:
        """Restore Ozzlan when his unfinished quest target is missing."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None or map_key(runtime.map_id) != "g0004_corrupted_jungle_01_pve":
            return
        quest_id = "g0003-TheCult-01-n"
        quest = self._quest_catalog().quest(quest_id)
        progress = next(
            (value for key, value in _quest_log(s).items()
             if key.casefold() == quest_id.casefold()),
            None,
        )
        if (
            quest is None
            or progress is None
            or progress.state != QUEST_STATE_ACCEPTED
        ):
            return
        target = next((
            task for task in quest.tasks
            if any(
                event.kind == "kill"
                and event.value.casefold() == "g0004_leader_badtribe_shaman_01"
                for event in task.events
            )
        ), None)
        if target is None or target.index not in {
            task.index for task in active_tasks(quest, progress)
        }:
            return
        combat = getattr(s, "gameplay_combat_state", None)
        if combat is not None:
            with combat.lock:
                _present, alive = combat.monster_template_counts()
            if alive.get("g0004_leader_badtribe_shaman_01".casefold(), 0) > 0:
                return
        # This authored guardian has a 120-second respawn in the map DB, but
        # ordinary fixed spawnpoints are only materialized at map creation.
        # Re-run the authored spawn row while the quest target is active;
        # the map-event duplicate guard keeps a living Ozzlan untouched.
        self._quest_spawn_monsters(s, (QuestSpawn(
            "g0004_leader_badtribe_shaman_01",
            "",
            0,
            "g0004_leader_badtribe_shaman_01_spawnpoint",
        ),))

    def _quest_ensure_ice_guardian(self, s: Session) -> None:
        """Restore the Ice Cradle quest guardian when its target is missing."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None or map_key(runtime.map_id) != "g0005_temple_ice_01_dun":
            return
        quest_id = "g0005-TempleOfIce-03-n"
        quest = self._quest_catalog().quest(quest_id)
        progress = next(
            (value for key, value in _quest_log(s).items()
             if key.casefold() == quest_id.casefold()),
            None,
        )
        if (
            quest is None
            or progress is None
            or progress.state != QUEST_STATE_ACCEPTED
        ):
            return
        target = next((
            task for task in quest.tasks
            if any(
                event.kind == "kill"
                and event.value.casefold() in {
                    "g0005_leader_guardian_ice_01",
                    "g0005_leader_guardian_ice_01_scale",
                }
                for event in task.events
            )
        ), None)
        if target is None or target.index not in {
            task.index for task in active_tasks(quest, progress)
        }:
            return
        combat = getattr(s, "gameplay_combat_state", None)
        templates = {
            "g0005_leader_guardian_ice_01",
            "g0005_leader_guardian_ice_01_scale",
        }
        if combat is not None:
            with combat.lock:
                existing_actor_ids = tuple(
                    actor_id for actor_id in combat.alive_actor_ids
                    if (
                        (monster := combat.monsters.get(actor_id)) is not None
                        and monster.template_id.casefold() in templates
                    )
                )
            if existing_actor_ids:
                self._quest_publish_ice_guardian(
                    s, runtime, combat, existing_actor_ids,
                )
                return
        # Fixed spawnpoints are materialized when a map is created. If the
        # guardian was missed then, restore the authored normal/scaled actor
        # at its corresponding spawnpoint while the kill task is active.
        try:
            difficulty = int(getattr(
                s, "gameplay_map_difficulty", runtime.scaling_difficulty,
            ))
        except (TypeError, ValueError):
            difficulty = int(runtime.scaling_difficulty)
        scaled = difficulty > 0
        self._quest_spawn_monsters(s, (QuestSpawn(
            (
                "g0005_leader_guardian_ice_01_scale"
                if scaled else "g0005_leader_guardian_ice_01"
            ),
            "",
            0,
            (
                "g0005_leader_guardian_ice_01_scale_spawnpoint"
                if scaled else "g0005_leader_guardian_ice_01_spawnpoint"
            ),
        ),))
        if combat is not None:
            with combat.lock:
                spawned_actor_ids = tuple(
                    actor_id for actor_id in combat.alive_actor_ids
                    if (
                        (monster := combat.monsters.get(actor_id)) is not None
                        and monster.template_id.casefold() in templates
                    )
                )
            self._quest_publish_ice_guardian(
                s, runtime, combat, spawned_actor_ids,
            )

    def _quest_ensure_lantern_scout(self, s: Session) -> None:
        """Spawn the missing Lantern 04 scout when its collect task is active."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None or map_key(runtime.map_id) != "h0006_valley_dun":
            return
        quest_id = "h0001-hunt-for-the-lantern-04-n"
        quest = self._quest_catalog().quest(quest_id)
        progress = next(
            (value for key, value in _quest_log(s).items()
             if key.casefold() == quest_id.casefold()),
            None,
        )
        if (
            quest is None
            or progress is None
            or progress.state != QUEST_STATE_ACCEPTED
        ):
            return
        template_id = "h0006_guardian_hunt_lantern_04_running_sandling_01"
        target = next((
            task for task in active_tasks(quest, progress)
            if any(
                event.kind == "kill"
                and event.value.casefold() == template_id.casefold()
                for event in task.events
            )
        ), None)
        if target is None:
            return
        collect_id = "h0006_hunt_for_the_lantern_04_lantern_piece_03"
        with s.flow_lock:
            if any(
                str(getattr(drop, "quest_id", "")).casefold()
                == quest_id.casefold()
                and str(getattr(drop, "quest_collect_id", "")).casefold()
                == collect_id.casefold()
                for drop in getattr(s, "gameplay_world_loot", {}).values()
            ):
                return
        combat = getattr(s, "gameplay_combat_state", None)
        if combat is not None:
            with combat.lock:
                if any(
                    (monster := combat.monsters.get(actor_id)) is not None
                    and monster.template_id.casefold() == template_id.casefold()
                    for actor_id in combat.alive_actor_ids
                ):
                    return
        # The map DB has no spawn row for the guardian variant. Reuse the
        # authored Running Sandling anchor; this is where the quest's scout
        # phase is staged, and the quest data supplies the kill/drop rules.
        self._quest_spawn_monsters(s, (QuestSpawn(
            template_id,
            "",
            0,
            "h0006_normal_hunt_lantern_04_running_sandling_normal_spawnpoint",
        ),))

    def _quest_publish_ice_guardian(
            self, s: Session, runtime, combat, actor_ids,
    ) -> None:
        """Force the active quest guardian to the client even across the map."""

        if not actor_ids:
            return
        grouped = {}
        for actor_id in actor_ids:
            group = combat.group_for_actor(actor_id)
            if group is not None:
                grouped.setdefault(id(group), (group, set()))[1].add(actor_id)
        sessions = self._map_event_sessions(runtime)
        if s not in sessions:
            sessions = (*sessions, s)
        for viewer in sessions:
            with viewer.flow_lock:
                ready = viewer.gameplay_presence_ready
            if not ready:
                continue
            for group, group_actor_ids in grouped.values():
                self._announce_runtime_group(
                    viewer, combat, group,
                    force_actor_ids=group_actor_ids,
                )

    def _quest_recover_myrdosch_access_key(self, s: Session) -> None:
        """Restore the Andrakas key when an older delivery lost its inventory drop."""

        quest_id = "f4101-TheGreatElevator-03-n"
        key_id = "key_from_f4101_to_f4100"
        with s.flow_lock:
            if map_key("f4100_hub") in s.gameplay_unlocked_maps:
                return
            inventory = s.gameplay_inventory
            if inventory is None or any(
                str(item.template_id).casefold() == key_id
                for item in inventory.items
            ):
                return
            progress = next(
                (value for quest, value in _quest_log(s).items()
                 if quest.casefold() == quest_id.casefold()),
                None,
            )
            if (
                progress is None
                or progress.state != QUEST_STATE_ACCEPTED
                or int(progress.tokens.get(
                    "TheGreatElevator-03-DeliverDeliveredLoot", 0
                )) < 1
            ):
                return
        quest = self._quest_catalog().quest(quest_id)
        if quest is None:
            return
        self._quest_grant_inventory_drops(s, quest, (
            Drop(key_id, 1, 1.0, True),
        ))
        CharacterService._request_account_save(
            s, "recovered Andrakas access key", flush=True,
        )

    def _quest_recover_artaya_sanctuary_key(self, s: Session) -> None:
        """Restore Artaya's key when its guardian was killed before the guaranteed drop."""

        quest_id = "g0003-TempleOfLife-01-n"
        key_id = "key_from_g0010_to_g0015"
        with s.flow_lock:
            if map_key("g0015_temple_life_md_01") in s.gameplay_unlocked_maps:
                return
            inventory = s.gameplay_inventory
            if inventory is None or any(
                str(item.template_id).casefold() == key_id
                for item in inventory.items
            ):
                return
            progress = next(
                (value for quest, value in _quest_log(s).items()
                 if quest.casefold() == quest_id.casefold()),
                None,
            )
            if (
                progress is None
                or progress.state != QUEST_STATE_ACCEPTED
                or int(progress.tokens.get(
                    "g0003-TempleOfLife-01-kill-01Kill", 0
                )) < 1
            ):
                return
        quest = self._quest_catalog().quest(quest_id)
        if quest is None:
            return
        self._quest_grant_inventory_drops(s, quest, (
            Drop(key_id, 1, 1.0, True),
        ))
        CharacterService._request_account_save(
            s, "recovered Artaya sanctuary key", flush=True,
        )

    # ------------------------------------------------------------------
    # Snapshots for the client
    # ------------------------------------------------------------------
    def _quest_log_info(self, s: Session) -> QuestLogInfo:
        catalog = self._quest_catalog()
        entries = []
        for quest_id, progress in _quest_log(s).items():
            quest = catalog.quest(quest_id)
            counters = task_counters(quest, progress) if quest else (0,) * MAX_TASKS
            entries.append(quest_log_entry(
                quest_id, progress.state, progress.finished_mask, counters,
                progress.completed_at,
            ))
        if os.getenv("DSO_NPC_TRACE") == "1":
            print(
                "[npc-trace] QuestLogInfo: "
                + ", ".join(
                    f"{quest_id}(state={progress.state},mask={progress.finished_mask})"
                    for quest_id, progress in _quest_log(s).items()
                )
            )
        return QuestLogInfo(tuple(entries))

    def _quest_chapter_progress(self, s: Session) -> ChapterProgress:
        catalog = self._quest_catalog()
        class_name, _level = _character_facts(s)
        log = _quest_log(s)
        chapters: dict[str, tuple[int, int]] = {}
        for quest_id in log:
            quest = catalog.quest(quest_id)
            if quest is None or not quest.chapter_id:
                continue
            if quest.chapter_id in chapters:
                continue
            members = catalog.chapter_quests(quest.chapter_id, class_name)
            closed = sum(
                1 for member in members
                if log.get(member.quest_id) is not None
                and log[member.quest_id].state == QUEST_STATE_CLOSED
            )
            chapters[quest.chapter_id] = (len(members), closed)
        acts: dict[str, tuple[int, int]] = {}
        for chapter_id, (total, closed) in chapters.items():
            act = _act_of(chapter_id)
            count, done = acts.get(act, (0, 0))
            acts[act] = (count + 1, done + (1 if total and closed >= total else 0))
        return chapter_progress(
            tuple((chapter, total, closed) for chapter, (total, closed) in chapters.items()),
            tuple((act, count, done) for act, (count, done) in acts.items()),
        )

    def _quest_signal_facts(
        self, s: Session,
    ) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
        """(accepted quests, solved quests, done tasks) for signal conditions."""

        catalog = self._quest_catalog()
        accepted, solved, tasks_done = set(), set(), set()
        for quest_id, progress in _quest_log(s).items():
            key = quest_id.casefold()
            if progress.state == QUEST_STATE_CLOSED:
                solved.add(key)
            else:
                accepted.add(key)
            quest = catalog.quest(quest_id)
            if quest is None:
                continue
            for task in quest.tasks:
                if progress.task_finished(task.index):
                    tasks_done.add(f"{quest_id}:{task.title}".casefold())
        return frozenset(accepted), frozenset(solved), frozenset(tasks_done)

    # ------------------------------------------------------------------
    # Quest-controlled props
    # ------------------------------------------------------------------
    @staticmethod
    def _quest_prop_operation(s: Session, definition) -> str | None:
        """The operation ``definition`` carries for this player; None hides it.

        A prop a task ``<Switch>`` names (the ``QuestControlled`` templates:
        quest bags, clue boxes, altars) exists for a player only while that
        quest is accepted, the owning task is open and the switch guards
        hold.  Retail sent those props in ``PropInfo`` with the switch id as
        operation and dropped them as the task advanced; an event quest's
        clue box in Kingshill must not show up for a player who cannot even
        take the quest.  Plain props return ``""``.
        """

        switch_id = str(getattr(definition, "quest_switch_id", "") or "")
        if not switch_id:
            return ""
        catalog = load_quest_catalog(DEFAULT_QUESTS_PATH)
        log = _quest_log(s)
        # Spawn-point props use the map row's placement label as instance_id
        # and the authored StateObjectId as template_id. Quest Switch.prop
        # names that template, so check both identities when resolving the
        # player's active switch; otherwise the prop is filtered from PropInfo.
        prop_ids = dict.fromkeys((
            str(getattr(definition, "instance_id", "") or "").casefold(),
            str(getattr(definition, "template_id", "") or "").casefold(),
        ))
        bindings = tuple(
            binding
            for prop_id in prop_ids if prop_id
            for binding in catalog.switches_for_prop(prop_id)
        )
        for binding in bindings:
            progress = log.get(binding.quest_id)
            if progress is None or progress.state != QUEST_STATE_ACCEPTED:
                continue
            quest = catalog.quest(binding.quest_id)
            if quest is None:
                continue
            for task, switch in active_prop_switches(quest, progress):
                if task.index == binding.task_index and switch == binding.switch:
                    return switch.switch_id
        # The parallel-world boss arena chests are also named by the
        # repeatable quest's final task. That task only becomes active after
        # the boss dies. Each arena publishes a visually identical,
        # non-QuestControlled template so players without the quest can still
        # see the locked chest; leave its operation empty for them.
        # MapEventService enforces the boss-death lock before consuming
        # IndividualUsage or awarding loot.
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        runtime_map = str(getattr(runtime, "map_id", "")).strip().casefold()
        if (
            runtime_map in _PARALLEL_WORLD_BOSS_REWARD_CHESTS
            and definition.instance_id.casefold()
            in PARALLEL_WORLD_REWARD_CHEST_INSTANCE_IDS
        ):
            return ""
        return None

    @staticmethod
    def _quest_prop_entries(s: Session, states) -> tuple:
        """``(state, operation)`` for the props ``s`` may see.

        Quest props that pass are remembered on the session so a later
        quest change can hide exactly what was shown.
        """

        shown = getattr(s, "gameplay_quest_props_visible", None)
        if shown is None:
            shown = set()
            s.gameplay_quest_props_visible = shown
        pairs = []
        for state in states:
            operation = QuestService._quest_prop_operation(s, state.definition)
            if operation is None:
                continue
            if operation:
                shown.add(int(state.definition.prop_id))
            pairs.append((state, operation))
        return tuple(pairs)

    def _quest_sync_props(self, s: Session) -> None:
        """Reveal or discard quest props as this player's quest state moves."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return
        with runtime.lock:
            states = [
                state for state in runtime.published_props()
                if state.definition.quest_switch_id
                and prop_visible_to_player(state.definition, s)
            ]
        # A one-shot quest prop may be authored for multiple stages in the
        # same chain. Once a later stage makes its switch live again, restore
        # the prop so the client can interact with it and the server can
        # process the new stage's switch.
        active_switch_props = {
            int(state.definition.prop_id)
            for state in states
            if state.definition.one_shot
            and self._quest_prop_operation(s, state.definition) is not None
        }
        reactivated_props = set()
        if active_switch_props:
            with runtime.lock:
                for prop_id in active_switch_props:
                    state = runtime.props.get(prop_id)
                    if state is not None and state.state == PROP_STATE_USED:
                        state.state = PROP_STATE_ACTIVE
                        reactivated_props.add(prop_id)
        shown = getattr(s, "gameplay_quest_props_visible", None)
        if shown is None:
            shown = set()
            s.gameplay_quest_props_visible = shown
        reveal, hide = [], []
        for state in states:
            prop_id = int(state.definition.prop_id)
            operation = self._quest_prop_operation(s, state.definition)
            if operation is None:
                if prop_id in shown:
                    shown.discard(prop_id)
                    hide.append(prop_id)
            elif prop_id not in shown or prop_id in reactivated_props:
                shown.add(prop_id)
                reveal.append((state, operation))
        if hide:
            packet, packet_bits = encode_discard_prop_batch(
                tuple(hide), actor_id=LOCAL_PLAYER_ACTOR_ID,
            )
            CharacterService._send_world_loot_state_batch(
                s, packet, packet_bits, label="quest-discard-prop",
            )
            _log(f"hid {len(hide)} quest prop(s) on {runtime.map_id}")
        if reveal:
            _send_prop_info_entries(s, tuple(reveal), label="quest-prop-info")
            _log(
                f"revealed {len(reveal)} quest prop(s) on {runtime.map_id}: "
                + ", ".join(sorted({
                    state.definition.instance_id for state, _ in reveal
                })[:4])
            )

    # ------------------------------------------------------------------
    # NPC dialogs
    # ------------------------------------------------------------------
    def _quest_npc_interactions(
        self, s: Session, npc_template_id: str,
    ) -> tuple[tuple[NPCInteraction, ...], tuple[NPCFlag, ...]]:
        catalog = self._quest_catalog()
        class_name, level = _character_facts(s)
        log = _quest_log(s)
        closed = [q for q, p in log.items() if p.state == QUEST_STATE_CLOSED]
        npc_key = str(npc_template_id).casefold()
        interactions: list[NPCInteraction] = []
        flags: list[NPCFlag] = []
        # Quests in progress first, as retail listed them (tutorial capture,
        # 16:04:10 and 16:08:29): while an open task has an encounter event
        # naming this NPC it carries the quest dialog (0) and the task-target
        # marker (1); once the quest is finished its goal NPC keeps the
        # dialog (0) for the turn-in.  A goal NPC whose encounter task is
        # not open yet gets nothing (the spellweaver at 16:05:47).
        for quest_id, progress in log.items():
            quest = catalog.quest(quest_id)
            if quest is None or progress.state == QUEST_STATE_CLOSED:
                continue
            targeted = any(
                (
                    task.task_type.casefold() == "completetask"
                    and quest.goal
                    and quest.goal.casefold() == npc_key
                    and task.is_done(progress.tokens)
                )
                or any(
                    event.kind == "encounter"
                    and event.value.casefold() == npc_key
                    and all(guard.holds(progress.tokens) for guard in event.guards)
                    for event in task.events
                )
                for task in active_tasks(quest, progress)
            )
            if targeted:
                interactions.append(NPCInteraction(
                    quest.quest_id, NPC_QUEST_DIALOG, quest.quest_id, quest.quest_type_id,
                ))
                interactions.append(NPCInteraction(
                    quest.quest_id, NPC_TASK_TARGET, quest.quest_id, quest.quest_type_id,
                ))
            elif (
                progress.state == QUEST_STATE_FINISHED
                and quest.goal.casefold() == npc_key
            ):
                interactions.append(NPCInteraction(
                    quest.quest_id, NPC_QUEST_DIALOG, quest.quest_id, quest.quest_type_id,
                ))
        active_events = self._quest_active_events(s)
        for quest in catalog.offered_by(npc_template_id):
            if catalog.available(
                quest, class_name=class_name, level=level,
                closed_quests=closed, known_quests=log.keys(),
                active_events=active_events,
            ):
                interactions.append(NPCInteraction(
                    quest.quest_id, NPC_QUEST_DIALOG, quest.quest_id, quest.quest_type_id,
                ))
        for quest_id, progress in log.items():
            quest = catalog.quest(quest_id)
            if quest is None:
                continue
            for task in active_tasks(quest, progress):
                for dialog_id, actor in task.dialogs.items():
                    if actor.casefold() != npc_key:
                        continue
                    talk_is_active = any(
                        event.kind == "talk"
                        and (
                            event.value.casefold() == dialog_id.casefold()
                            or dialog_id.casefold().endswith(
                                event.value.casefold()
                            )
                        )
                        and all(
                            guard.holds(progress.tokens)
                            for guard in event.guards
                        )
                        for event in task.events
                    )
                    if talk_is_active:
                        # TalkTask targets use the dialog interaction to open
                        # the conversation and a separate task-target entry
                        # for the NPC quest marker. Without both, the client
                        # can show the objective text while leaving its map
                        # target unmarked.
                        interactions.append(NPCInteraction(
                            quest.quest_id, NPC_TASK_TARGET,
                            quest.quest_id, quest.quest_type_id,
                        ))
                    interactions.append(NPCInteraction(
                        dialog_id, NPC_TALK_DIALOG, quest.quest_id, quest.quest_type_id,
                    ))
                    flags.append(NPCFlag(dialog_id, False))
        for quest, barter in self._quest_npc_barters(s, npc_template_id):
            # The delivery action opens the item hand-in UI, while this
            # separate target entry drives the quest marker over the NPC.
            interactions.append(NPCInteraction(
                quest.quest_id, NPC_TASK_TARGET,
                quest.quest_id, quest.quest_type_id,
            ))
            interactions.append(NPCInteraction(
                barter.barter_id, NPC_DELIVERY_DIALOG, quest.quest_id, quest.quest_type_id,
            ))
        return tuple(interactions), tuple(flags)

    def _quest_npc_barters(self, s: Session, npc_template_id: str):
        catalog = self._quest_catalog()
        npc_key = str(npc_template_id).casefold()
        return tuple(
            (quest, barter)
            for quest_id, progress in _quest_log(s).items()
            if (quest := catalog.quest(quest_id)) is not None
            for task in active_tasks(quest, progress)
            for barter in task.barters
            if barter.actor.casefold() == npc_key
            and barter.live(progress.tokens)
        )

    def _quest_decorate_npc_info(
        self, s: Session, npc_info: NPCInfo, npc_template_id: str,
    ) -> NPCInfo:
        interactions, flags = self._quest_npc_interactions(s, npc_template_id)
        barters = tuple(
            NPCStringSet(barter.barter_id, barter.items, barter.amount, barter.reply)
            for _quest, barter in self._quest_npc_barters(s, npc_template_id)
        )
        if not interactions and not flags and not barters:
            return npc_info
        return replace(
            npc_info,
            interactions=npc_info.interactions + interactions,
            flags=npc_info.flags + flags,
            string_sets=npc_info.string_sets + barters,
        )

    def _quest_decorate_map_commands(self, s: Session, commands) -> tuple:
        """Add quest offers to the NPCInfo records of a map initialization."""

        try:
            definitions = load_local_npc_definitions(
                self.static_database_path,
                s.gameplay_map or DEFAULT_GAMEPLAY_MAP,
                include_inactive_event_sets=True,
            )
        except Exception as exc:
            _log(f"NPC definitions unavailable: {type(exc).__name__}: {exc}")
            return tuple(commands)
        by_actor = {}
        for definition in definitions:
            # Match the row-ordered actor published by NewNPC/NPCInfo.
            by_actor.setdefault(local_npc_actor_id(definition), definition.template_id)
        decorated = []
        for command in commands:
            if command.command_id == NPC_INFO_COMMAND:
                template = by_actor.get(int(command.value.actor_id))
                if template:
                    command = ServerCommand(
                        command.command_id,
                        self._quest_decorate_npc_info(s, command.value, template),
                        command.actor_id,
                    )
            decorated.append(command)
        if os.getenv("DSO_NPC_TRACE") == "1":
            print(
                "[npc-trace] decorated init NPCInfo: "
                + ", ".join(
                    f"actor{command.value.actor_id}"
                    f"/{by_actor.get(int(command.value.actor_id), '?')}"
                    f"[cat={command.value.category:#04x} "
                    f"tpl={command.value.template_id!r} "
                    f"inter={[(i.field_08, i.source, i.target) for i in command.value.interactions]}]"
                    for command in decorated
                    if command.command_id == NPC_INFO_COMMAND
                )
            )
        return tuple(decorated)

    def _quest_npc_info_commands(self, s: Session) -> tuple[ServerCommand, ...]:
        """NPCInfo for the map's live quest NPCs, as retail re-sends them.

        Retail put the refresh inside the quest batch itself: the accept
        reply was ``QuestUpdate, NPCInfo x3, ChapterProgress`` (16:04:10.562)
        and the encounter reply ``QuestUpdate(task), NPCInfo x3,
        QuestUpdate(state), NPCInfo x3`` (16:04:33.381).
        """

        try:
            definitions = CharacterService._active_npc_definitions(self, s)
        except Exception as exc:
            _log(f"NPC refresh skipped: {type(exc).__name__}: {exc}")
            return ()
        # Every live NPC, quest-related or not: retail's refresh after the
        # tutorial accept included the far-away invisible NPC (65536) with
        # an empty offer list.
        commands = []
        for definition in definitions:
            template = definition.template_id
            info = self._quest_decorate_npc_info(
                s, local_npc_info(definition), template,
            )
            commands.append(ServerCommand(
                NPC_INFO_COMMAND, info,
                int(getattr(info, "actor_id", definition.actor_id)),
            ))
        return tuple(commands)

    def _quest_refresh_npc_infos(self, s: Session) -> None:
        """Send the NPCInfo refresh on its own (outside a quest batch)."""

        self._quest_spawn_revealed_npcs(s)
        self._send_quest_commands(
            s, self._quest_npc_info_commands(s), label="quest-npc-info",
        )

    def _quest_spawn_revealed_npcs(self, s: Session) -> None:
        """Send NewNPC for quest-gated targets the client has no actor for.

        NPCInfo refreshes alone cannot reveal a gated NPC (hub smith_01 /
        spellweaver_01, Kingshill event vendors): the client drops NPCInfo
        for an actor it never created. This sends the missing NewNPC+NPCInfo
        pair per player, mirroring the map-event path, and records the actor
        so a later refresh does not resend a duplicate id.
        """

        try:
            definitions = CharacterService._active_npc_definitions(self, s)
        except Exception as exc:
            _log(f"revealed NPC spawn skipped: {type(exc).__name__}: {exc}")
            return
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        map_local_event_flags = set()
        event_set_flags = {}
        if runtime is not None:
            try:
                event_set_flags = load_local_event_set_flags(
                    self.static_database_path,
                    s.gameplay_map or DEFAULT_GAMEPLAY_MAP,
                )
                for flags in event_set_flags.values():
                    for flag in flags:
                        event = runtime.catalog.event(str(flag).casefold())
                        if (
                            event is not None
                            and event.applies_to(runtime.map_id)
                            and not event.client_only
                        ):
                            map_local_event_flags.add(str(flag).casefold())
            except Exception as exc:
                _log(
                    f"map-local NPC gate lookup failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        with s.flow_lock:
            announced = set(
                getattr(s, "gameplay_announced_npc_actor_ids", set()) or set()
            )
            missing = tuple(
                definition for definition in definitions
                if int(local_npc_actor_id(definition)) not in announced
                and int(getattr(definition, "event_set_row", -1)) >= 0
                # Server-driven map-local events own their NewNPC emission.
                # Client-only scene events (such as Wael's reveal) can show
                # the model without registering a network interaction actor;
                # quest refreshes must create those actors themselves.
                and not (
                    event_set_flags.get(
                        int(definition.event_set_row), frozenset()
                    )
                    & map_local_event_flags
                )
            )
            claimed_actor_ids = {
                int(local_npc_actor_id(definition)) for definition in missing
            }
            # Event transitions and quest refreshes can discover the same
            # newly enabled NPC on different threads. Claim its actor id
            # before releasing the lock so only one sender emits NewNPC.
            s.gameplay_announced_npc_actor_ids.update(claimed_actor_ids)
        if not missing:
            return
        commands: list = []
        actor_ids = []
        for definition in missing:
            actor_id = int(local_npc_actor_id(definition))
            actor_ids.append(actor_id)
            commands.append(NewNPC(
                template_id=definition.template_id,
                instance_guid=definition.instance_guid,
                placement=ActorPlacement(
                    LOCAL_NPC_ACTOR_BASE, actor_id, 1, False, False
                ),
                rotation=definition.rotation,
                position=definition.position,
                enabled=True,
                actor_id=actor_id,
            ))
            commands.append(CharacterService._quest_decorate_npc_info(
                self, s,
                local_npc_info(definition, actor_id=actor_id),
                definition.template_id,
            ))
        try:
            vicinity_packet, vicinity_bits = encode_actors_enter_vicinity_batch(
                ActorsEnterVicinityBatch(
                    ActorsEnterVicinity(tuple(actor_ids)),
                    actor_id=0,
                    commands=(),
                )
            )
            s.send_reliable_ordered(
                vicinity_packet,
                track=True,
                label="quest-revealed-npc-actor-vicinity",
            )
            packet, packet_bits = encode_npc_batch(
                NPCBatch(tuple(commands)), include_actor_vicinity=False,
            )
        except Exception as exc:
            with s.flow_lock:
                s.gameplay_announced_npc_actor_ids.difference_update(
                    claimed_actor_ids
                )
            _log(f"revealed NPC encode failed: {type(exc).__name__}: {exc}")
            return
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label="quest-revealed-npc",
        )
        _log(
            f"revealed {len(missing)} quest-gated NPC(s) on "
            f"{s.gameplay_map!r}: "
            + ", ".join(sorted({d.template_id for d in missing})[:6])
        )

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    def _send_quest_commands(self, s: Session, commands, *, label: str) -> None:
        commands = tuple(commands)
        if not commands:
            return
        packet, packet_bits = encode_server_command_batch(
            commands, include_terminator=True,
        )
        CharacterService._send_world_loot_state_batch(
            s, packet, packet_bits, label=label,
        )

    def _quest_state_commands(self, s: Session, quest_id: str, state: int, stamp: str):
        """Retail's state batch: the update, the NPCInfo refresh, the chapter."""

        return (
            ServerCommand(
                QUEST_UPDATE_COMMAND,
                QuestUpdate.quest_state(quest_id, state, stamp),
                LOCAL_PLAYER_ACTOR_ID,
            ),
            *self._quest_npc_info_commands(s),
            ServerCommand(
                CHAPTER_PROGRESS_COMMAND,
                self._quest_chapter_progress(s),
                LOCAL_PLAYER_ACTOR_ID,
            ),
        )

    # ------------------------------------------------------------------
    # Client commands
    # ------------------------------------------------------------------
    def _handle_quest_command(self, s: Session, payload: bytes) -> None:
        request = decode_quest_command(
            payload, getattr(s, "current_message_bit_length", None)
        )
        with s.flow_lock:
            s.gameplay_client_command_state[QUEST_COMMAND] = request
        CharacterService._accept_gameplay_state_command(s, QUEST_COMMAND)
        if request.operation == QUEST_OPERATION_ACCEPT:
            self._quest_accept(s, request.quest_id, offered=True)
        elif request.operation == QUEST_OPERATION_TURN_IN:
            self._quest_turn_in(s, request.quest_id)
        elif request.operation == QUEST_OPERATION_CANCEL:
            self._quest_cancel(s, request.quest_id)
        else:
            _log(f"unknown QuestCommand operation {request.operation} for {request.quest_id!r}")

    def _handle_talk_command(self, s: Session, payload: bytes) -> None:
        request = decode_talk(payload, getattr(s, "current_message_bit_length", None))
        with s.flow_lock:
            s.gameplay_client_command_state[TALK_COMMAND] = request
        CharacterService._accept_gameplay_state_command(s, TALK_COMMAND)
        definition = CharacterService._resolve_npc_interaction(self, s, request.npc_actor_id)
        if definition is None:
            _log(f"talk references unknown local actor {request.npc_actor_id}")
            return
        _log(f"talk {request.dialog_id!r} at actor {request.npc_actor_id}")
        self._quest_on_npc_encounter(s, definition.template_id)
        changed = self._quest_feed(s, "talk", request.dialog_id)
        if changed:
            # The talk response can finish the previous task and unlock a
            # CompleteTask at this same NPC. Its initial encounter was
            # processed before the talk token existed, so resolve the newly
            # unlocked hand-in task now.
            npc_key = definition.template_id.casefold()
            catalog = self._quest_catalog()
            for quest_id, progress in tuple(_quest_log(s).items()):
                quest = catalog.quest(quest_id)
                if quest is None or progress.state != QUEST_STATE_ACCEPTED:
                    continue
                if any(
                    task.task_type.casefold() == "completetask"
                    and quest.goal.casefold() == npc_key
                    and task.is_done(progress.tokens)
                    for task in active_tasks(quest, progress)
                ):
                    self._quest_feed(
                        s, "encounter", definition.template_id, quest_id=quest_id,
                    )

    @staticmethod
    def _quest_delivery_items(inventory, barter):
        """Native hand-ins count carried storage, never bank/equipped/socketed items."""
        if inventory is None:
            return ()
        carried = set(inventory.storage_58) | set(inventory.storage_80) | set(inventory.field_180)
        excluded = set(inventory.equipment_slots) | set(inventory.storage_a8) | set(inventory.storage_d0)
        order = {template.casefold(): index for index, template in enumerate(barter.items)}
        return tuple(sorted(
            (item for item in inventory.items
             if item.item_id in carried and item.item_id not in excluded
             and item.template_id.casefold() in order and int(item.amount) > 0),
            key=lambda item: (order[item.template_id.casefold()], item.item_id),
        ))

    def _handle_deliver_command(self, s: Session, payload: bytes) -> None:
        request = decode_deliver(payload, getattr(s, "current_message_bit_length", None))
        CharacterService._accept_gameplay_state_command(s, DELIVER_COMMAND)
        definition = CharacterService._resolve_npc_interaction(self, s, request.npc_actor_id)
        error = "unknown local NPC"
        committed = None
        with s.flow_lock:
            s.gameplay_client_command_state[DELIVER_COMMAND] = request
            if (not s.connected or not s.gameplay_presence_ready
                    or s.gameplay_presence_departed or s.gameplay_exit_transition_started):
                return
            if definition is not None:
                choice = next(((quest, barter) for quest, barter
                               in self._quest_npc_barters(s, definition.template_id)
                               if barter.barter_id == request.barter_id), None)
                error = "barter is not an active task at this NPC"
                if choice is not None:
                    quest, barter = choice
                    items = self._quest_delivery_items(s.gameplay_inventory, barter)
                    error = "required items are not in carried inventory"
                    if barter.amount > 0 and sum(int(item.amount) for item in items) >= barter.amount:
                        progress = copy.deepcopy(_quest_log(s)[quest.quest_id])
                        result = apply_quest_event(quest, progress, "deliver", barter.barter_id)
                        error = "delivery event guards do not allow progress"
                        if result.changes or result.quest_finished:
                            builder = InventoryMutationBuilder(
                                s.gameplay_inventory, s.gameplay_next_item_id,
                            )
                            remaining = barter.amount
                            if not barter.only_show:
                                for item in items:
                                    amount = min(remaining, int(item.amount))
                                    builder.consume(item.item_id, amount, preserve_item_id=True)
                                    remaining -= amount
                                    if not remaining:
                                        break
                            mutation = builder.finish()
                            # One commit: an account snapshot cannot save the
                            # consumed items without their completed objective.
                            s.gameplay_inventory = mutation.inventory
                            s.gameplay_next_item_id = mutation.next_item_id
                            _quest_log(s)[quest.quest_id] = progress
                            committed = (quest, barter, result, mutation)
            # The client predicts item consumption even for show-only tasks.
            # A rejected/show-only hand-in must restore its authoritative bag.
            inventory_snapshot = copy.deepcopy(s.gameplay_inventory)
        if committed is None:
            _log(f"delivery {request.barter_id!r} rejected: {error}")
            if inventory_snapshot is not None:
                self._send_quest_commands(s, (ServerCommand(
                    INVENTORY_INFO_COMMAND, inventory_snapshot, LOCAL_PLAYER_ACTOR_ID,
                ),), label="quest-delivery-rejected")
            self._quest_refresh_npc_infos(s)
            return
        quest, barter, result, mutation = committed
        inventory_records = (inventory_snapshot,) if barter.only_show else (mutation.delta, mutation.final)
        self._send_quest_commands(s, tuple(ServerCommand(
            INVENTORY_INFO_COMMAND, record, LOCAL_PLAYER_ACTOR_ID,
        ) for record in inventory_records), label="quest-delivery-inventory")
        self._quest_publish_result(s, quest, result, "deliver", barter.barter_id)
        if result.inventory_drops:
            self._quest_grant_inventory_drops(
                s, quest, result.inventory_drops,
            )
        self._quest_spawn_drops(s, quest, result.drops)
        self._quest_sync_props(s)
        CharacterService._request_account_save(s, "quest item delivered", flush=True)

    def _handle_quest_monster_indication_request(self, s: Session, payload: bytes) -> None:
        request = decode_quest_indication_request(
            payload, QUEST_MONSTER_INDICATION_REQUEST_COMMAND,
            getattr(s, "current_message_bit_length", None),
        )
        CharacterService._accept_gameplay_state_command(
            s, QUEST_MONSTER_INDICATION_REQUEST_COMMAND
        )
        with s.flow_lock:
            s.gameplay_quest_tracking = (request.quest_id, int(request.task_index))
        # A region is a world-state condition, not ownership of the final
        # killing blow. Recover on a tracker refresh if a death came through
        # a path without a player kill callback; never trust a client count.
        self._quest_check_cleared_regions(s)
        self._quest_send_monster_indication(s)

    def _handle_quest_trigger_indication_request(self, s: Session, payload: bytes) -> None:
        request = decode_quest_indication_request(
            payload, QUEST_TRIGGER_INDICATION_REQUEST_COMMAND,
            getattr(s, "current_message_bit_length", None),
        )
        CharacterService._accept_gameplay_state_command(
            s, QUEST_TRIGGER_INDICATION_REQUEST_COMMAND
        )
        quest = self._quest_catalog().quest(request.quest_id)
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        triggers = []
        if quest is not None and runtime is not None:
            task = next(
                (t for t in quest.tasks if t.index == int(request.task_index)), None
            )
            wanted = {
                event.value.casefold() for event in (task.events if task else ())
                if event.kind == "trigger"
            }
            for volume in runtime.content.trigger_volumes:
                if volume.triggered_event_id.casefold() in wanted:
                    m = volume.matrix
                    triggers.append(QuestTriggerIndication(
                        volume.triggered_event_id, (m[12], m[13], m[14]),
                    ))
        self._send_quest_commands(s, (ServerCommand(
            QUEST_TRIGGER_INDICATION_INFO_COMMAND,
            QuestTriggerIndicationInfo(
                request.quest_id, int(request.task_index), tuple(triggers),
            ),
            LOCAL_PLAYER_ACTOR_ID,
        ),), label="quest-trigger-indication")

    def _quest_send_monster_indication(self, s: Session) -> None:
        tracking = getattr(s, "gameplay_quest_tracking", None)
        if not tracking:
            return
        quest_id, task_index = tracking
        quest = self._quest_catalog().quest(quest_id)
        if quest is None:
            return
        task = next((t for t in quest.tasks if t.index == task_index), None)
        templates = {
            event.value.casefold() for event in (task.events if task else ())
            if event.kind == "kill"
        }
        if task is not None:
            templates.update(
                template.strip().casefold()
                for monsters_in_region in task.clear_regions.values()
                for template in monsters_in_region.split(";") if template.strip()
            )
        monsters = []
        combat = getattr(s, "gameplay_combat_state", None)
        if combat is not None and templates:
            with combat.lock:
                for actor_id in sorted(combat.alive_actor_ids):
                    monster = combat.monsters.get(actor_id)
                    if monster is None:
                        continue
                    template = str(getattr(monster, "template_id", ""))
                    if template.casefold() not in templates:
                        continue
                    position = combat.monster_positions.get(actor_id, monster.position)
                    monsters.append(QuestMonsterIndication(
                        int(actor_id), template,
                        tuple(float(v) for v in position[:3]),
                    ))
        if "pw011" in str(getattr(s, "gameplay_map", "")).casefold():
            summary = [
                (
                    m.actor_id,
                    m.template_id,
                    tuple(round(v, 1) for v in m.position),
                )
                for m in monsters
            ]
            _log(
                "pw011 monster indication "
                f"quest={quest_id!r} task={task_index} actors={summary}"
            )
        tick = int(getattr(s, "gameplay_chase_server_tick", 0) or 0) & 0xFFFFFFFF
        self._send_quest_commands(s, (ServerCommand(
            QUEST_MONSTER_INDICATION_INFO_COMMAND,
            QuestMonsterIndicationInfo(tick, quest_id, task_index, tuple(monsters)),
            LOCAL_PLAYER_ACTOR_ID,
        ),), label="quest-monster-indication")

    # ------------------------------------------------------------------
    # Accept / turn in / cancel
    # ------------------------------------------------------------------
    def _quest_accept(
        self, s: Session, quest_id: str, *, offered: bool,
        commit_accept: Callable[[], bool] | None = None,
    ) -> bool:
        """Accept and optionally commit board state under the same flow lock.

        commit_accept must only validate/update in-memory state; no I/O.
        Returning False leaves the quest log untouched.
        """
        catalog = self._quest_catalog()
        quest = catalog.quest(quest_id)
        if quest is None:
            _log(f"accept of unknown quest {quest_id!r} ignored")
            return False
        if quest.quest_type == "wantedrepeatable" and commit_accept is None:
            _log(f"Wanted quest {quest.quest_id!r} requires a board acceptance")
            return False
        class_name, level = _character_facts(s)
        log = _quest_log(s)
        closed = [q for q, p in log.items() if p.state == QUEST_STATE_CLOSED]
        if not catalog.available(
            quest, class_name=class_name, level=level,
            closed_quests=closed, known_quests=log.keys(),
            active_events=self._quest_active_events(s),
        ):
            _log(f"quest {quest.quest_id!r} is not available to this character")
            return False
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if quest.bound_to_map:
            if runtime is None or (offered and quest.auto_start):
                return False
            maps = frozenset().union(*(task.maps for task in quest.tasks)) or quest.maps
            if maps and str(s.gameplay_map).casefold() not in maps:
                return False
            with runtime.lock:
                if self._quest_run_key(s, quest) in runtime.completed_quests:
                    return False
        with s.flow_lock:
            if s.gameplay_exit_transition_started:
                return False
            existing = log.get(quest.quest_id)
            if existing is not None and existing.state != QUEST_STATE_CLOSED:
                return False
            progress = QuestProgress(quest.quest_id)
            if quest.bound_to_map:
                progress.map_instance = runtime.instance_id
            if commit_accept is not None and not commit_accept():
                return False
            log[quest.quest_id] = progress
        _log(f"accepted {quest.quest_id!r} ({'offered' if offered else 'auto-start'})")
        commands = list(self._quest_state_commands(
            s, quest.quest_id, QUEST_STATE_ACCEPTED, NO_TIMESTAMP,
        ))
        # ChapterUnlocked only the first time this character sees the
        # chapter; the set is persisted (services/achievements.py).
        commands.extend(
            CharacterService._chapter_unlock_commands(self, s, quest.chapter_id)
        )
        self._send_quest_commands(s, commands, label="quest-accept")
        if quest.accept_inventory_drops:
            self._quest_grant_inventory_drops(
                s, quest, quest.accept_inventory_drops,
            )
        # Accepting a quest can reveal a client-side map EventSet while the
        # player is already on that map. Publish its event state immediately.
        try:
            if self._quest_reveal_flags(s, s.gameplay_map or ""):
                self._quest_refresh_event_state(s)
        except Exception as exc:
            _log(
                f"quest event refresh after acceptance failed: "
                f"{type(exc).__name__}: {exc}"
            )
        self._quest_spawn_revealed_npcs(s)
        self._quest_refresh_map_phases(s)
        self._quest_sync_props(s)
        self._quest_start_active_holdgrounds(s)
        self._quest_ensure_cult_shaman(s)
        self._quest_ensure_ice_guardian(s)
        self._quest_ensure_lantern_scout(s)
        CharacterService._request_account_save(s, "quest accepted")
        self._quest_fire_signal(s, "PlayerOpenedQuest", (quest.quest_id,))
        return True

    def _quest_turn_in(self, s: Session, quest_id: str) -> bool:
        catalog = self._quest_catalog()
        quest = catalog.quest(quest_id)
        log = _quest_log(s)
        progress = log.get(quest.quest_id) if quest else None
        if quest is None or progress is None:
            _log(f"turn-in of unknown quest {quest_id!r} ignored")
            return False
        if progress.state == QUEST_STATE_CLOSED:
            _log(f"quest {quest.quest_id!r} is already closed")
            return False
        if progress.state != QUEST_STATE_FINISHED and not all(
            progress.task_finished(task.index) for task in quest.tasks
        ):
            _log(f"quest {quest.quest_id!r} has unfinished tasks; turn-in refused")
            return False
        with s.flow_lock:
            if (s.gameplay_exit_transition_started or log.get(quest.quest_id) is not progress
                    or progress.state == QUEST_STATE_CLOSED):
                return False
            # Quest loot carries provenance while it is in the bag. Consume
            # those items in the same locked hand-in transition; for older
            # saves, the cleanup helper also recognizes unambiguous QuestItems.
            mutation = self._quest_cancel_inventory(s, quest)
            if mutation is not None:
                s.gameplay_inventory = mutation.inventory
                s.gameplay_next_item_id = mutation.next_item_id
            progress.state = QUEST_STATE_CLOSED
            progress.completed_at = _now_stamp()
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if quest.bound_to_map and runtime is not None:
            with runtime.lock:
                runtime.completed_quests.add(self._quest_run_key(s, quest))
        CharacterService._quest_unlock_maps(self, s, quest.quest_id)
        _log(f"closed {quest.quest_id!r}")
        if mutation is not None:
            self._send_quest_commands(s, tuple(ServerCommand(
                INVENTORY_INFO_COMMAND, record, LOCAL_PLAYER_ACTOR_ID,
            ) for record in (mutation.delta, mutation.final)), label="quest-turn-in-items")
        self._quest_grant_rewards(s, quest)
        self._send_quest_commands(s, self._quest_state_commands(
            s, quest.quest_id, QUEST_STATE_CLOSED, progress.completed_at,
        ), label="quest-turn-in")
        # Closing a prerequisite can make an EventSet-gated follow-up giver
        # available on the same map. Publish the new per-character reveal
        # state before sending the NPC refresh.
        try:
            if self._quest_reveal_flags(s, s.gameplay_map or ""):
                self._quest_refresh_event_state(s)
        except Exception as exc:
            _log(
                f"quest event refresh after turn-in failed: "
                f"{type(exc).__name__}: {exc}"
            )
        self._quest_spawn_revealed_npcs(s)
        self._quest_refresh_map_phases(s)
        self._quest_sync_props(s)
        CharacterService._request_account_save(s, "quest closed")
        self._quest_fire_signal(s, "PlayerClosedQuest", (quest.quest_id,))
        if quest.key == "f4100-railroadbarrier-02-n":
            CharacterService._quest_start_f4102_train_repair(self, s)
        CharacterService._achievement_on_quest_closed(self, s, quest.quest_id)
        return True

    def _quest_cancel_inventory(self, s: Session, quest: QuestDefinition):
        """Build cleanup under flow_lock; commit it with the quest removal."""
        inventory = s.gameplay_inventory
        if inventory is None:
            return None
        items = tuple(owned_inventory_items(inventory))
        remove = {item.item_id for item in items if item.quest_id.casefold() == quest.key}
        # Older saves have no provenance. Only infer dedicated QuestItem
        # ownership, and retain ambiguous items used by another open quest.
        legacy = _quest_item_templates(quest)
        catalog = self._quest_catalog()
        for other_id, progress in _quest_log(s).items():
            if other_id.casefold() == quest.key or progress.state == QUEST_STATE_CLOSED:
                continue
            other = catalog.quest(other_id)
            if other is not None:
                legacy.difference_update(_quest_item_templates(other))
        for template_id in {item.template_id for item in items
                            if not item.quest_id and item.template_id.casefold() in legacy}:
            try:
                definition = load_purchase_item_definition(self.static_database_path, template_id)
            except ValueError:
                continue
            if definition.item_category == "QuestItem":
                remove.update(item.item_id for item in items
                              if not item.quest_id and item.template_id == template_id)
        if not remove:
            return None
        builder = InventoryMutationBuilder(inventory, s.gameplay_next_item_id)
        removed_ingredients = {item.template_id for item in inventory.items
                               if item.item_id in remove and item.item_id in inventory.field_180}
        for item in inventory.items:
            if item.item_id in remove:
                builder.consume(item.item_id)
        for name in ("premium_inventory_overflow", "premium_storage_overflow"):
            setattr(builder.inventory, name, [item for item in getattr(builder.inventory, name)
                                              if item.item_id not in remove])
        remaining_ingredients = {item.template_id for item in builder.inventory.items
                                 if item.item_id in builder.inventory.field_180}
        builder.inventory.field_1a0 = [template for template in builder.inventory.field_1a0
                                      if template not in removed_ingredients - remaining_ingredients]
        return builder.finish()

    def _quest_cancel(self, s: Session, quest_id: str, *, force: bool = False,
                      notify: bool = True) -> bool:
        quest = self._quest_catalog().quest(quest_id)
        if quest is None or (not force and not quest.deletable):
            return False
        quest_id = quest.quest_id
        with s.flow_lock:
            log = _quest_log(s)
            progress = log.get(quest_id)
            if progress is None or progress.state == QUEST_STATE_CLOSED:
                return False
            mutation = self._quest_cancel_inventory(s, quest)
            if mutation is not None:
                s.gameplay_inventory = mutation.inventory
                s.gameplay_next_item_id = mutation.next_item_id
            # Remove ground items in the same transaction so an in-flight
            # pickup cannot add an item after its inventory cleanup.
            removed_drops = [item_id for item_id, drop in s.gameplay_world_loot.items()
                             if drop.quest_id.casefold() == quest.key]
            for item_id in removed_drops:
                del s.gameplay_world_loot[item_id]
            del log[quest_id]
            tracking = getattr(s, "gameplay_quest_tracking", None)
            if tracking and str(tracking[0]).casefold() == quest.key:
                s.gameplay_quest_tracking = None
        _log(f"cancelled {quest_id!r}")
        if notify:
            if removed_drops:
                CharacterService._reconcile_world_loot_visibility(s, label="quest-drop-cancel")
            if mutation is not None:
                self._send_quest_commands(s, tuple(ServerCommand(
                    INVENTORY_INFO_COMMAND, record, LOCAL_PLAYER_ACTOR_ID,
                ) for record in (mutation.delta, mutation.final)), label="quest-cancel-inventory")
            self._send_quest_commands(s, self._quest_state_commands(
                s, quest_id, QUEST_STATE_CANCELLED, NO_TIMESTAMP,
            ), label="quest-cancel")
            self._quest_sync_props(s)
        CharacterService._request_account_save(s, "quest cancelled")
        return True

    def _quest_grant_rewards(self, s: Session, quest: QuestDefinition) -> None:
        class_name, _level = _character_facts(s)
        xp = 0
        gold = 0
        andermant = 0
        loot = []
        for reward in quest.rewards_for_class(class_name):
            try:
                amount = int(float(reward.value)) if reward.kind in ("xp", "vc", "rc") else int(reward.amount)
            except ValueError:
                amount = 0
            if reward.kind == "xp":
                xp += amount
            elif reward.kind == "vc":
                gold += amount
            elif reward.kind == "rc":
                andermant += amount
            elif reward.kind == "loot" and reward.value:
                loot.append(RewardDefinition(
                    f"quest:{quest.quest_id}:{reward.value}", reward.value, max(1, amount),
                ))
            elif reward.kind == "dynamicxp":
                xp += self._quest_dynamic_xp(s, float(reward.value or 0))
            elif reward.kind == "statuseffect":
                if amount or reward.value:
                    _log(f"reward {reward.kind} {reward.value!r} of {quest.quest_id!r} is not implemented")
        if xp > 0:
            try:
                CharacterService._award_experience(
                    s, xp, database_path=self.static_database_path, label="quest-xp",
                )
            except Exception as exc:
                _log(f"xp reward failed: {type(exc).__name__}: {exc}")
        if gold > 0 or andermant > 0:
            self._quest_credit_currency(s, gold=gold, andermant=andermant)
        if loot:
            try:
                self._deliver_rewards(s, tuple(loot), claim_keys=None, label="quest-loot")
            except Exception as exc:
                _log(f"loot reward failed: {type(exc).__name__}: {exc}")

    def _quest_dynamic_xp(self, s: Session, percent: float) -> int:
        if percent <= 0:
            return 0
        with s.flow_lock:
            character = s.gameplay_character
            if character is None:
                return 0
            class_id, level = int(character.class_id), int(character.level)
            points = tuple(s.gameplay_talent_category_points)
            current = max(int(getattr(character, "progression_u32", 0)),
                          int(points[0]) if len(points) == 4 else 0)
            effects = snapshot_active_status_entries(
                s.gameplay_persistent_status_effects, s.gameplay_timed_status_effect_expiries,
                s.gameplay_runtime_status_effects, s.gameplay_item_set_effects, s.gameplay_socket_effects)
        amounts = evaluate_active_status_modifiers(
            load_status_modifier_catalog(self.static_database_path), effects).quest_experience
        multiplier = max(0.0, 1.0 + amounts.relative + amounts.absolute)
        return load_player_progression_catalog(self.static_database_path).dynamic_quest_xp(
            class_id=class_id, current_level=level, current_xp=current,
            percent=percent, multiplier=multiplier)

    def _quest_credit_currency(self, s: Session, *, gold: int, andermant: int) -> None:
        with s.flow_lock:
            current = getattr(s, "gameplay_currency_state", None)
            if current is None:
                _log("currency reward skipped: currency state unavailable")
                return
            updated = current
            try:
                if gold > 0:
                    updated = _credit_currency(updated, "money_vc_gold", int(gold))
                if andermant > 0:
                    updated = _credit_currency(updated, "money_rc", int(andermant))
            except ValueError as exc:
                _log(f"currency reward failed: {exc}")
                return
            s.gameplay_currency_state = updated
        self._send_quest_commands(s, (ServerCommand(
            CURRENCY_CHANGED_COMMAND, updated, LOCAL_PLAYER_ACTOR_ID,
        ),), label="quest-currency")

    # ------------------------------------------------------------------
    # World events
    # ------------------------------------------------------------------
    def _quest_feed(self, s: Session, kind: str, value: str, *, count: int = 1,
                    quest_id: str = "", position=None) -> bool:
        """Apply one world event to every active quest; returns whether any changed."""

        if not value or s.gameplay_exit_transition_started:
            return False
        catalog = self._quest_catalog()
        log = _quest_log(s)
        changed_any = False
        selected_ids = (quest_id,) if quest_id else tuple(log)
        for quest_id in selected_ids:
            progress = log.get(quest_id)
            if progress is None or progress.state != QUEST_STATE_ACCEPTED:
                continue
            quest = catalog.quest(quest_id)
            if quest is None:
                continue
            result = apply_quest_event(quest, progress, kind, value, count=count)
            for unsupported in result.unsupported:
                _log(f"{quest_id!r} needs unsupported event {unsupported!r}")
            if result.spawns:
                try:
                    self._quest_spawn_monsters(s, result.spawns)
                except Exception as exc:
                    _log(
                        f"quest monster spawn failed for {quest_id!r}: "
                        f"{type(exc).__name__}: {exc}"
                    )
            if result.inventory_drops:
                self._quest_grant_inventory_drops(
                    s, quest, result.inventory_drops,
                )
                changed_any = True
            if result.drops:
                self._quest_spawn_drops(s, quest, result.drops, position=position)
                CharacterService._request_account_save(s, "quest drop event")
                changed_any = True
            if result.changes or result.quest_finished:
                changed_any = True
                self._quest_publish_result(s, quest, result, kind, value)
        if changed_any:
            if str(kind).casefold() != "trigger":
                self._quest_reconcile_explore_trigger_presence(s)
            self._quest_sync_props(s)
            if getattr(s, "gameplay_quest_tracking", None):
                self._quest_send_monster_indication(s)
        return changed_any

    def _quest_grant_inventory_drops(self, s, quest, specifications) -> None:
        """Grant authored inventory=true quest drops as real inventory items."""

        rewards = tuple(
            RewardDefinition(
                f"quest:{quest.quest_id}:{spec.item_id}",
                quest_item_template_id(spec.item_id),
                int(spec.amount),
            )
            for spec in specifications
            if spec.item_id and int(spec.amount) > 0
        )
        if not rewards:
            return
        try:
            self._deliver_rewards(
                s, rewards, claim_keys=None, label="quest-item",
            )
        except Exception as exc:
            _log(
                f"inventory quest drop failed for {quest.quest_id!r}: "
                f"{type(exc).__name__}: {exc}"
            )

    def _quest_spawn_drops(self, s, quest, specifications, *, position=None):
        if not specifications:
            return
        with s.flow_lock:
            if s.gameplay_exit_transition_started:
                return
            progress = _quest_log(s).get(quest.quest_id)
            if progress is None:
                return
            generation = s.gameplay_chase_generation
            map_id = str(s.gameplay_map or "")
            instance_key = CharacterService._gameplay_instance_key(s)
            if position is None:
                encounter = s.gameplay_monster_encounter
                move = s.gameplay_last_move
                position = (encounter.coordinate_frame.wire_to_world(move.relative_position)
                            if encounter is not None and move is not None
                            else getattr(s, "gameplay_spawn_position", None) or (0., 0., 0.))
        drops = []
        for spec in specifications:
            try:
                drops.extend(build_quest_world_drops(
                    self.static_database_path, quest.quest_id, spec.item_id,
                    spec.amount, position,
                ))
            except ValueError as exc:
                _log(f"cannot materialize drop for {quest.quest_id!r}: {exc}")
        if not drops:
            return
        with s.flow_lock:
            next_item_id = s.gameplay_next_item_id
        # Share the ordinary loot allocator's high-water mark; personal drops
        # belonging to different players must still have distinct client IDs.
        with self.gameplay_world_lock:
            counters = getattr(self, "gameplay_world_next_item_ids", None)
            if counters is None:
                counters = self.gameplay_world_next_item_ids = {}
            first = max(next_item_id, counters.get(instance_key, RUNTIME_ITEM_ID_BASE))
            end = first + len(drops)
            counters[instance_key] = end
            counters[map_id] = max(end, counters.get(map_id, RUNTIME_ITEM_ID_BASE))
        with s.flow_lock:
            if (generation != s.gameplay_chase_generation or s.gameplay_exit_transition_started
                    or _quest_log(s).get(quest.quest_id) is not progress):
                return
            for offset, drop in enumerate(drops):
                drop.item.item_id = first + offset
                drop.item.field_18 = drop.item.item_id & 0xFFFF
                s.gameplay_world_loot[drop.item.item_id] = drop
            s.gameplay_next_item_id = max(s.gameplay_next_item_id, end)
        CharacterService._reconcile_world_loot_visibility(s, label="quest-drop")

    def _quest_refresh_event_state(self, s: Session) -> None:
        """Publish quest-derived map EventSet flags after task transitions."""

        try:
            update, _progress, _participation = self._event_initialization_state(s)
            packet, packet_bits = encode_event_update(update)
            CharacterService._send_world_loot_state_batch(
                s, packet, packet_bits, label="quest-map-event-state",
            )
        except Exception as exc:
            _log(
                f"map event refresh after quest progress failed: "
                f"{type(exc).__name__}: {exc}"
            )

    def _quest_publish_result(self, s, quest, result, kind, value):
        """Publish already-committed task progress and its map signals."""
        commands = []
        for change in result.changes:
            commands.append(ServerCommand(
                QUEST_UPDATE_COMMAND,
                QuestUpdate.task_progress(
                    quest.quest_id, change.task.index, change.progress,
                    change.target, finished=change.finished,
                ),
                LOCAL_PLAYER_ACTOR_ID,
            ))
        if result.quest_finished or any(c.finished for c in result.changes):
            # Retail (16:04:33.381): the NPCInfo refresh follows the task
            # record, and again after the state record.
            commands.extend(self._quest_npc_info_commands(s))
        if result.quest_finished:
            commands.append(ServerCommand(
                QUEST_UPDATE_COMMAND,
                QuestUpdate.quest_state(quest.quest_id, QUEST_STATE_FINISHED, NO_TIMESTAMP),
                LOCAL_PLAYER_ACTOR_ID,
            ))
            commands.extend(self._quest_npc_info_commands(s))
        if result.quest_finished or any(c.finished for c in result.changes):
            # Quest targets can unlock a client-side map EventSet while the
            # player is already on the map. Refresh its event flags before
            # sending NPCInfo, so newly revealed quest NPCs actually appear.
            self._quest_refresh_event_state(s)
        _log(
            f"{quest.quest_id!r} {kind} {value!r}: "
            + ", ".join(
                f"task {c.task.index} {c.progress}/{c.target}{' done' if c.finished else ''}"
                for c in result.changes
            )
            + (" -> finished" if result.quest_finished else "")
        )
        self._send_quest_commands(s, commands, label="quest-progress")
        self._quest_ensure_cult_shaman(s)
        self._quest_ensure_ice_guardian(s)
        self._quest_spawn_revealed_npcs(s)
        self._quest_refresh_map_phases(s)
        self._quest_ensure_lantern_scout(s)
        for change in result.changes:
            if change.finished:
                self._quest_fire_signal(
                    s, "PlayerEndQuestTask", (f"{quest.quest_id}:{change.task.title}",),
                )
        if result.quest_finished and quest.key in {
            "g0004-enterminibossroom-01-n-row",
            "g0004-enterminibossroom-01-n",
            "g0004-enterminibossroom-02-n-row",
            "g0004-enterminibossroom-03-n-row",
        }:
            # These miniboss-room quests finish at their NPC, and their
            # Alliance Hall key is a quest reward. Close them immediately so
            # the reward is delivered as soon as the final NPC task completes.
            self._quest_turn_in(s, quest.quest_id)
        elif result.quest_finished and quest.bound_to_map and not quest.goal:
            # Task signals reveal the earned chests before the quest closes.
            self._quest_turn_in(s, quest.quest_id)
        CharacterService._request_account_save(s, "quest progress")

    def _quest_on_npc_encounter(self, s: Session, npc_template_id: str) -> None:
        self._quest_feed(s, "encounter", npc_template_id)

    def _quest_on_monsters_killed(self, s: Session, monsters) -> None:
        counts: dict[str, int] = {}
        for monster in monsters:
            template = str(getattr(monster, "template_id", ""))
            if template:
                counts[template] = counts.get(template, 0) + 1
                if template.casefold() in {
                    "g0010_leader_guardian_life_01",
                    "g0010_leader_guardian_life_01_scale",
                }:
                    # The guardian's special loot list is not included in
                    # exported server data. Guarantee its sanctuary key on
                    # the authoritative player-kill callback.
                    self._deliver_rewards(
                        s,
                        (RewardDefinition(
                            "monster:g0010_artaya_sanctuary_key",
                            "key_from_g0010_to_g0015",
                            1,
                        ),),
                        claim_keys=None,
                        label="monster-loot",
                    )
                    CharacterService._request_account_save(
                        s, "Artaya sanctuary key drop", flush=True,
                    )
                combat = s.gameplay_combat_state
                position = getattr(monster, "position", None)
                if combat is not None:
                    with combat.lock:
                        position = combat.monster_positions.get(
                            getattr(monster, "actor_id", None), position)
                self._quest_feed(s, "kill", template, position=position)
        self._quest_check_shared_cleared_regions(s, counts)

    def _quest_start_active_holdgrounds(self, s: Session) -> None:
        """Complete designated defense stages when their quest is accepted."""

        map_quests = {
            "c2302_dun": ("c2200-TempleOfEternity-01-n",),
            # J'Udora, J'Edile, and J'Oronis column defense events cannot run
            # in this reconstruction. Complete the three sequential steps
            # after Rewrite History is accepted so J'Ibal can be reached.
            "c2303_dun": ("c2200-RewriteHistory-01-n",),
            # The railroad defense scene is not simulated; taking its first
            # quest should count Lukas as defended and unlock the follow-up.
            "f4102_dun": ("f4100-RailroadBarrier-01-n",),
            # Arnox's protection scene is not simulated; accepting this quest
            # should mark its authored hold-ground objective as defended.
            "f4201_pve": ("f4201-NotSafeAnymore-03-n",),
            # The imprisoned Technikus and Janus Fox defense scenes are not
            # simulated; accept their authored hold-ground objectives when
            # the quests start.
            "f4202_dun": (
                "f4100-DestroyWorldsHeart-01-n",
                "f4100-DestroyWorldsHeart-06-n",
            ),
            # Kavlin Lefve's defense scene is not simulated; accepting the
            # quest should mark its authored hold-ground objective complete.
            "g0001_beach_01_dun": ("g0001-InvestigateCorruption-01-n",),
            # Yohuac's defense scene is not simulated; accepting the quest
            # should complete its hold-ground objective before the collection
            # tasks become active.
            "g0002_jungle_01_dun": ("g0002-TheTribe-03-n",),
            # The Opal Shrine guardian defense scene is not simulated; taking
            # the quest in Gün Batımı should count the guardian as defended.
            "g0004_corrupted_jungle_01_pve": ("g0004-TempleOfWater-01-n",),
            # Okeanos's guardian defense scene inside the Opal Shrine is also
            # not simulated; accepting its sixth chapter quest counts it done.
            "g0006_temple_water_01_dun": ("g0006-TempleOfWater-08-n",),
            # High Priest Azlan's ritual defense in Fjalnir's Temple is not
            # simulated; accepting the quest should count Azlan as defended.
            "g0005_temple_ice_02_dun": ("g0005-TempleOfIce-08-n",),
            # Shaman Tletelli's defense in Fyrgon's Fire Path is not
            # simulated; accepting the quest should count her as defended.
            "g0007_temple_fire_01_dun": ("g0007-TempleOfFire-04-n",),
        }
        map_id = map_key(getattr(s, "gameplay_map", ""))
        for quest_id in map_quests.get(map_id, ()):
            quest = self._quest_catalog().quest(quest_id)
            if quest is None:
                continue
            # Completing one holdground task may unlock the next sequential
            # task, so refresh active tasks and progress after each signal.
            for _ in range(len(quest.tasks)):
                progress = _quest_log(s).get(quest_id)
                if progress is None or progress.state != QUEST_STATE_ACCEPTED:
                    break
                task = next((
                    task for task in active_tasks(quest, progress)
                    if task.task_type.casefold() == "holdgroundtask"
                ), None)
                if task is None:
                    break
                event = next((
                    event for event in task.events
                    if event.kind == "holdgroundsucceeded"
                ), None)
                if event is None:
                    break
                before = progress.finished_mask
                self._quest_feed(
                    s, "holdgroundsucceeded", event.value,
                    quest_id=quest_id,
                )
                updated = _quest_log(s).get(quest_id)
                if updated is None or updated.finished_mask == before:
                    break

    def _quest_send_tracked_clear_region_indication(
        self, s: Session, *, group_only: bool = False,
    ) -> None:
        """Refresh the visible remaining-mob list from authoritative map state."""

        tracking = getattr(s, "gameplay_quest_tracking", None)
        if not tracking:
            return
        quest_id, task_index = tracking
        quest = self._quest_catalog().quest(quest_id)
        progress = next((value for key, value in _quest_log(s).items()
                         if key.casefold() == str(quest_id).casefold()), None)
        if quest is None or progress is None or progress.state != QUEST_STATE_ACCEPTED:
            return
        map_keys = {
            str(value).casefold()
            for value in (
                s.gameplay_map,
                getattr(s, "gameplay_map_access_id", ""),
                getattr(s, "gameplay_map_access_group_id", ""),
            )
            if value
        }
        task = next((value for value in active_tasks(quest, progress)
                     if value.index == int(task_index)
                     and value.clear_regions
                     and (not value.maps or bool(map_keys.intersection(
                         item.casefold() for item in value.maps
                     )))
                     and (not group_only or value.group)), None)
        if task is not None:
            self._quest_send_monster_indication(s)

    def _quest_check_shared_cleared_regions(self, s: Session, killed: dict[str, int]) -> None:
        # Send the new living population before a final kill advances and
        # closes the clear-region task. The client's counter is derived from
        # this actor list, so it must receive the empty list at zero as well.
        self._quest_send_tracked_clear_region_indication(s)
        self._quest_check_cleared_regions(s, killed)
        # ClearRegion describes the shared map population. A group-enabled
        # objective must also finish for participants who did not land the
        # final hit, including when the killer has no active quest at all.
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        combat = getattr(s, "gameplay_combat_state", None)
        if runtime is not None and combat is not None:
            for peer in self._map_event_sessions(runtime):
                if peer is not s and peer.gameplay_combat_state is combat:
                    self._quest_send_tracked_clear_region_indication(
                        peer, group_only=True,
                    )
                    self._quest_check_cleared_regions(peer, killed, group_only=True)

    def _quest_check_cleared_regions(
            self, s: Session, killed: dict[str, int] | None = None,
            *, group_only: bool = False) -> None:
        catalog = self._quest_catalog()
        combat = getattr(s, "gameplay_combat_state", None)
        if combat is None:
            return
        with combat.lock:
            present, alive = combat.monster_template_counts()
        alive_keys = {str(k).casefold() for k, v in alive.items() if v}
        if killed is None:
            killed = {template: count for template, count in present.items()
                      if count > 0 and not alive.get(template, 0)}
        killed_keys = {k.casefold() for k in killed}
        for quest_id, progress in list(_quest_log(s).items()):
            quest = catalog.quest(quest_id)
            if quest is None or progress.state != QUEST_STATE_ACCEPTED:
                continue
            for task in active_tasks(quest, progress):
                if group_only and not task.group:
                    continue
                if task.maps and str(s.gameplay_map).casefold() not in task.maps:
                    continue
                for region_id, monster in task.clear_regions.items():
                    templates = {value.strip().casefold() for value in monster.split(";") if value.strip()}
                    if templates.intersection(killed_keys) and not templates.intersection(alive_keys):
                        self._quest_feed(s, "regioncleared", region_id)

    def _quest_on_item_collected(self, s: Session, template_id: str, amount: int = 1,
                                 *, quest_id: str = "") -> None:
        self._quest_feed(s, "collect", template_id, count=max(1, int(amount)),
                         quest_id=quest_id)

    def _quest_on_prop_interacted(self, s: Session, instance_id: str, operation: str) -> None:
        catalog = self._quest_catalog()
        switch_id = str(operation or "") or catalog.switch_props.get(
            str(instance_id).casefold(), ""
        )
        if switch_id:
            changed = self._quest_feed(s, "interact", switch_id)
            if changed:
                self._send_quest_commands(s, (ServerCommand(
                    QUEST_UPDATE_COMMAND, QuestUpdate.interaction(), LOCAL_PLAYER_ACTOR_ID,
                ),), label="quest-interaction")

    def _quest_on_triggers(self, s: Session, trigger_ids) -> None:
        for trigger_id in trigger_ids:
            self._quest_feed(s, "trigger", str(trigger_id))

    def _quest_reconcile_explore_trigger_presence(self, s: Session) -> None:
        """Credit a newly active ExploreTask if its trigger already contains the player."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return
        map_id = str(getattr(runtime, "map_id", None) or "").casefold()
        if not map_id:
            return
        with runtime.lock:
            volumes = runtime.active_trigger_volumes()
        with s.flow_lock:
            inside = set(getattr(s, "gameplay_trigger_volumes_inside", ()))
        if not inside:
            return
        triggers_inside = {
            str(volume.triggered_event_id or "").casefold()
            for volume in volumes
            if volume.row_id in inside and volume.triggered_event_id
        }
        if not triggers_inside:
            return
        for quest_id, progress in tuple(_quest_log(s).items()):
            quest = self._quest_catalog().quest(quest_id)
            if quest is None or progress.state != QUEST_STATE_ACCEPTED:
                continue
            for task in active_tasks(quest, progress):
                if task.maps and map_id not in task.maps:
                    continue
                trigger_id = explore_trigger_for_task(quest, task).casefold()
                if trigger_id and trigger_id in triggers_inside:
                    self._quest_feed(
                        s, "trigger", trigger_id, quest_id=quest.quest_id,
                    )

    def _quest_on_signal(self, s: Session, signal_id: str) -> None:
        """Start the giver-less quests that a fired signal opens."""

        for quest_id in tuple(_quest_log(s)):
            quest = self._quest_catalog().quest(quest_id)
            if quest and quest.stop_signal and quest.stop_signal.casefold() == str(signal_id).casefold():
                self._quest_cancel(s, quest_id, force=True)
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        rule = runtime.catalog.rule(signal_id) if runtime is not None else None
        if rule is not None and rule.event_type in {"playerenteredmap", "playerenteredmapid"}:
            if not self._quest_entry_signal_matches(s, rule):
                return
        for quest in self._quest_catalog().started_by(signal_id):
            if quest.auto_start:
                self._quest_accept(s, quest.quest_id, offered=False)

    def _quest_fire_signal(self, s: Session, event_type: str, params) -> None:
        fire = getattr(CharacterService, "_fire_map_signal", None)
        if fire is None:
            return
        try:
            fire(self, s, event_type, params)
        except Exception as exc:
            _log(f"signal {event_type} failed: {type(exc).__name__}: {exc}")

    def _quest_start_f4102_train_repair(self, s: Session) -> None:
        """Persist the authored train/gate scene transition after quest 02."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None or map_key(runtime.map_id) != "f4102_dun":
            return
        definition = runtime.catalog.event("f4102_train_repair")
        if definition is None or not definition.applies_to(runtime.map_id):
            return
        with runtime.lock:
            transition = runtime.begin_event(definition)
        if transition is not None:
            CharacterService._apply_map_event_transition(
                self, runtime, transition,
            )

    def _quest_refresh_map_phases(self, s: Session) -> None:
        """Re-evaluate map-entry phasing after quest state changed mid-map.

        Entry rules (e.g. hub_02: 02-n accepted + garden unlocked) only fire
        on map entry. A turn-in on the same map can satisfy them afterwards
        (garden unlocks while standing in the hub); without a re-entry the
        phased NPCs would never spawn. Already-running events no-op.
        """

        try:
            runtime = getattr(s, "gameplay_map_event_runtime", None)
            map_id = str(getattr(runtime, "map_id", None) or s.gameplay_map or "")
            if not runtime or not map_id:
                return
            access_group = str(getattr(s, "gameplay_map_access_group_id", "") or "")
            access_id = str(getattr(s, "gameplay_map_access_id", "") or "")
            self._quest_fire_signal(s, "PlayerEnteredMap", (map_id, access_group, access_id))
            self._quest_fire_signal(s, "PlayerEnteredMapId", (access_group, access_id, map_id))
        except Exception as exc:
            _log(f"map phase refresh failed: {type(exc).__name__}: {exc}")


__all__ = ["QuestService"]
