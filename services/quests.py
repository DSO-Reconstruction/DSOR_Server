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
    load_local_npc_definitions,
    local_npc_actor_id,
    local_npc_info,
)
from dso_server.gameplay.quest_content import (
    MAX_TASKS,
    NO_TIMESTAMP,
    QUEST_STATE_ACCEPTED,
    QUEST_STATE_CLOSED,
    QUEST_STATE_CANCELLED,
    QUEST_STATE_FINISHED,
    QuestCatalog,
    QuestDefinition,
    QuestProgress,
    active_prop_switches,
    active_tasks,
    apply_quest_event,
    load_quest_catalog,
    task_counters,
)
from dso_server.paths import DEFAULT_QUESTS_PATH
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.gameplay_protocol import (
    NPC_INFO_COMMAND,
    NPCBatch,
    NPCFlag,
    NPCInfo,
    NPCInteraction,
    NPCStringSet,
    encode_npc_batch,
)
from dso_server.protocol.loot_protocol import _credit_currency
from dso_server.protocol.prop_protocol import encode_discard_prop_batch
from dso_server.protocol.player_init_protocol import (
    CURRENCY_CHANGED_COMMAND,
    ChapterProgress,
    QuestLogInfo,
    ServerCommand,
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
from dso_server.services.map_events import _send_prop_info_entries


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
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return
        # These entry signals are authored as client reports. The server knows
        # the same facts and can start the quest even if that report is lost.
        for quest in self._quest_catalog().quests.values():
            if not quest.bound_to_map or not quest.auto_start:
                continue
            rule = runtime.catalog.rule(quest.start_signal)
            if rule is not None and rule.event_type in {"playerenteredmap", "playerenteredmapid"}:
                if self._quest_entry_signal_matches(s, rule):
                    self._quest_on_signal(s, rule.signal_id)
        # Shared dungeons may already be cleared when another player joins.
        # Reconcile against the actual population, never an empty spawn table.
        self._quest_check_cleared_regions(s)

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
        for binding in catalog.switches_for_prop(definition.instance_id):
            progress = log.get(binding.quest_id)
            if progress is None or progress.state != QUEST_STATE_ACCEPTED:
                continue
            quest = catalog.quest(binding.quest_id)
            if quest is None:
                continue
            for task, switch in active_prop_switches(quest, progress):
                if task.index == binding.task_index and switch == binding.switch:
                    return switch.switch_id
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
            ]
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
            elif prop_id not in shown:
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
                event.kind == "encounter"
                and event.value.casefold() == npc_key
                and all(guard.holds(progress.tokens) for guard in event.guards)
                for task in active_tasks(quest, progress)
                for event in task.events
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
                    interactions.append(NPCInteraction(
                        dialog_id, NPC_TALK_DIALOG, quest.quest_id, quest.quest_type_id,
                    ))
                    flags.append(NPCFlag(dialog_id, False))
        for quest, barter in self._quest_npc_barters(s, npc_template_id):
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
            if barter.actor.casefold() == npc_key and barter.live(progress.tokens)
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
            # Match the actor published by NewNPC/NPCInfo. Arena merchants
            # use a separate actor range from their static row-derived IDs.
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

        self._send_quest_commands(
            s, self._quest_npc_info_commands(s), label="quest-npc-info",
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
        self._quest_feed(s, "talk", request.dialog_id)

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
        self._quest_sync_props(s)
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
            progress.state = QUEST_STATE_CLOSED
            progress.completed_at = _now_stamp()
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if quest.bound_to_map and runtime is not None:
            with runtime.lock:
                runtime.completed_quests.add(self._quest_run_key(s, quest))
        CharacterService._quest_unlock_maps(self, s, quest.quest_id)
        _log(f"closed {quest.quest_id!r}")
        self._quest_grant_rewards(s, quest)
        self._send_quest_commands(s, self._quest_state_commands(
            s, quest.quest_id, QUEST_STATE_CLOSED, progress.completed_at,
        ), label="quest-turn-in")
        self._quest_sync_props(s)
        CharacterService._request_account_save(s, "quest closed")
        self._quest_fire_signal(s, "PlayerClosedQuest", (quest.quest_id,))
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
            if result.drops:
                self._quest_spawn_drops(s, quest, result.drops, position=position)
                CharacterService._request_account_save(s, "quest drop event")
                changed_any = True
            if result.changes or result.quest_finished:
                changed_any = True
                self._quest_publish_result(s, quest, result, kind, value)
        if changed_any:
            self._quest_sync_props(s)
            if getattr(s, "gameplay_quest_tracking", None):
                self._quest_send_monster_indication(s)
        return changed_any

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
        _log(
            f"{quest.quest_id!r} {kind} {value!r}: "
            + ", ".join(
                f"task {c.task.index} {c.progress}/{c.target}{' done' if c.finished else ''}"
                for c in result.changes
            )
            + (" -> finished" if result.quest_finished else "")
        )
        self._send_quest_commands(s, commands, label="quest-progress")
        for change in result.changes:
            if change.finished:
                self._quest_fire_signal(
                    s, "PlayerEndQuestTask", (f"{quest.quest_id}:{change.task.title}",),
                )
        if result.quest_finished and quest.bound_to_map and not quest.goal:
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
                combat = s.gameplay_combat_state
                position = getattr(monster, "position", None)
                if combat is not None:
                    with combat.lock:
                        position = combat.monster_positions.get(
                            getattr(monster, "actor_id", None), position)
                self._quest_feed(s, "kill", template, position=position)
        self._quest_check_shared_cleared_regions(s, counts)

    def _quest_check_shared_cleared_regions(self, s: Session, killed: dict[str, int]) -> None:
        self._quest_check_cleared_regions(s, killed)
        # ClearRegion describes the shared map population. A group-enabled
        # objective must also finish for participants who did not land the
        # final hit, including when the killer has no active quest at all.
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        combat = getattr(s, "gameplay_combat_state", None)
        if runtime is not None and combat is not None:
            for peer in self._map_event_sessions(runtime):
                if peer is not s and peer.gameplay_combat_state is combat:
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
                    elif templates.intersection(killed_keys) and (
                        getattr(s, "gameplay_quest_tracking", None) == (quest_id, task.index)
                    ):
                        self._quest_send_monster_indication(s)

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


__all__ = ["QuestService"]
