"""CMS bridge to current gameplay sessions, native rewards and transfers."""

import copy
from contextlib import closing
from dataclasses import replace
from functools import lru_cache
import json
import sqlite3

from dso_server.gameplay.local_map_state import map_database_path
from dso_server.gameplay.map_difficulty_state import resolve_map_access
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.protocol.currency_protocol import GAME_CURRENCY_BY_NAME, GAME_CURRENCY_CHANGED_COMMAND, GameCurrencyChanged
from dso_server.protocol.inventory_codec import INVENTORY_INFO_COMMAND
from dso_server.protocol.world_loot_protocol import EQUIPMENT_RARITIES
from dso_server.protocol.player_init_protocol import (
    CURRENCY_CHANGED_COMMAND, ServerCommand, Notification, make_notification_entry, encode_server_command_batch,
    TALENT_CATEGORY_POINTS_INFO_COMMAND, TALENT_SELECTION_INFO_COMMAND,
    TalentCategoryPointsInfo, TalentSelectionInfo,
)
from dso_server.protocol.pvp_match_protocol import (
    HonorPointsChanged, encode_honor_points_changed,
)
from dso_server.protocol.gameplay_commands import LOCAL_PLAYER_ACTOR_ID
from dso_server.protocol.reward_protocol import (
    NOTIFICATION_COMMAND, OCP_BOOKING_NOTIFICATION_KIND, OCP_BOOKING_NOTIFICATION_VALUES,
    OCPBookingReward, ocp_booking_notification_payload,
)
from dso_server.protocol.skill_book_protocol import experience_commands
from dso_server.runtime.admin_commands import AdminCommandQueue, call_admin_command
from dso_server.services.admin_effects import ADMIN_EFFECTS, matching_admin_effects
from dso_server.services.base import CharacterService
from dso_server.services.customization import CustomizationService
from dso_server.services.map_unlocks import MapUnlockService
from dso_server.services.player_persistence import PlayerPersistenceService
from dso_server.services.player_progression import PlayerXPTransition, player_level_xp


BOOKING_GUID_PREFIX = b"DSOCMS01"
_CURRENCY_TEMPLATES = {"andermant": "money_rc", "gold": "money_vc",
                       "badge_of_honor": "money_hc_no_auto_use", "draken": "e_chr_2012_event_currency",
                       "glyph_of_power": "money_gc_no_auto_use"}


def ready(s, cid):
    return (s.connected and not getattr(s, "retired", False) and s.gameplay_mode
            and s.gameplay_character is not None and s.gameplay_character.character_id == cid
            and s.gameplay_initialization_sent and s.gameplay_presence_ready
            and not s.gameplay_presence_departed and not s.gameplay_exit_transition_started)


def state_token(s):
    """Read under flow_lock; economy structures are copy-on-write."""
    inventory = s.gameplay_inventory
    return (id(inventory), s.gameplay_currency_state, tuple(sorted(s.gameplay_game_currencies.items())),
            s.gameplay_next_item_id, tuple(s.gameplay_admin_booking_ids),
            s.gameplay_map, id(s.gameplay_combat_state), s.gameplay_chase_generation,
            inventory.health_points if inventory else None,
            inventory.secondary_resource if inventory else None)


@lru_cache(maxsize=4)
def map_catalog(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT DISTINCT MapTemplate,AccessGroup,MapType FROM _Template_MapTemplate").fetchall()
        accesses = db.execute("SELECT Id,AccessGroup,Difficulty FROM _Template_MapAccessGroup").fetchall()
    groups = {str(group): (str(physical), str(kind)) for physical, group, kind in rows if physical}
    result = {str(physical): {"map_id": str(physical), "physical_map": str(physical),
                              "type": str(kind), "difficulty": 0}
              for physical, _, kind in rows if physical and map_database_path(path, str(physical)).is_file()}
    for access, group, difficulty in accesses:
        physical, kind = groups.get(str(group), (str(group), ""))
        if map_database_path(path, physical).is_file():
            result[str(access)] = {"map_id": str(access), "physical_map": physical,
                                   "type": kind, "difficulty": int(difficulty or 0)}
    return tuple(result[key] for key in sorted(result))


def booking_packet(after, booking, mutation, details=None):
    commands = []
    resource = booking["resource"]
    notifications = []
    if booking["kind"] == "batch":
        entries = json.loads(resource)
        batch_details = (details or {}).get("batch", [])
        mutations = [entry.get("mutation") for entry in batch_details if entry.get("mutation") is not None]
        changed = {item.item_id for current in mutations for item in current.changed_items}
        replaced = [item_id for current in mutations for item_id in current.replaced_item_ids]
        replacements = {key: value for current in mutations for key, value in current.replacements.items()}
        if changed:
            delta = copy.deepcopy(after.inventory)
            delta.items = [item for item in delta.items if item.item_id in changed]
            delta.changed_item_ids = replaced
            delta.field_158 = replacements
            delta.field_1e0 = True
            final = copy.deepcopy(after.inventory)
            final.items, final.changed_item_ids, final.field_158, final.field_1e0 = [], [], {}, False
            commands.extend((ServerCommand(INVENTORY_INFO_COMMAND, delta, 1),
                             ServerCommand(INVENTORY_INFO_COMMAND, final, 1)))
        fixed_currency_changed = False
        for entry in entries:
            entry_resource = entry["resource"]
            if entry["kind"] == "item":
                rarity = EQUIPMENT_RARITIES[(entry.get("options") or {}).get("rarity", 0)]
                notifications.append((entry_resource, entry["amount"], rarity))
            elif entry_resource in GAME_CURRENCY_BY_NAME:
                wire_id = GAME_CURRENCY_BY_NAME[entry_resource].wire_id
                notifications.append((wire_id, entry["amount"], "Common"))
            else:
                notifications.append((_CURRENCY_TEMPLATES[entry_resource], entry["amount"], "Common"))
                fixed_currency_changed = True
        affected_game_currencies = {
            GAME_CURRENCY_BY_NAME[entry["resource"]].wire_id
            for entry in entries
            if entry["kind"] == "currency" and entry["resource"] in GAME_CURRENCY_BY_NAME
        }
        for wire_id in sorted(affected_game_currencies):
            commands.append(ServerCommand(GAME_CURRENCY_CHANGED_COMMAND,
                                          GameCurrencyChanged(wire_id, after.game_currencies[wire_id]), 1))
        if fixed_currency_changed:
            commands.append(ServerCommand(CURRENCY_CHANGED_COMMAND, replace(after.currency, notify=True), 1))
    elif booking["kind"] == "item":
        changed = {item.item_id for item in mutation.changed_items}
        delta = copy.deepcopy(after.inventory)
        delta.items = [item for item in delta.items if item.item_id in changed]
        delta.changed_item_ids = list(mutation.replaced_item_ids)
        delta.field_158 = dict(mutation.replacements)
        delta.field_1e0 = True
        final = copy.deepcopy(after.inventory)
        final.items, final.changed_item_ids, final.field_158, final.field_1e0 = [], [], {}, False
        commands.extend((ServerCommand(INVENTORY_INFO_COMMAND, delta, 1),
                         ServerCommand(INVENTORY_INFO_COMMAND, final, 1)))
        notifications.append((resource, booking["amount"], EQUIPMENT_RARITIES[(booking.get("options") or {}).get("rarity", 0)]))
    elif resource in GAME_CURRENCY_BY_NAME:
        template = GAME_CURRENCY_BY_NAME[resource].wire_id
        commands.append(ServerCommand(GAME_CURRENCY_CHANGED_COMMAND,
                                       GameCurrencyChanged(template, after.game_currencies[template]), 1))
        notifications.append((template, booking["amount"], "Common"))
    else:
        template = _CURRENCY_TEMPLATES[resource]
        commands.append(ServerCommand(CURRENCY_CHANGED_COMMAND, replace(after.currency, notify=True), 1))
        notifications.append((template, booking["amount"], "Common"))
    notification_entries = []
    for index, (template, amount, rarity) in enumerate(notifications):
        # Keep each grant visible as an individual notification while sharing
        # the booking's durable idempotency receipt.
        guid_id = booking["id"] if index == 0 else (booking["id"] * 64 + index) & 0xFFFFFFFFFFFFFFFF
        guid = BOOKING_GUID_PREFIX + guid_id.to_bytes(8, "big")
        payload = ocp_booking_notification_payload(OCPBookingReward("cms", template, amount, rarity), guid)
        # The grant has already been committed. Acknowledging this notice must
        # never trigger another credit or require the player to accept a reward.
        payload["ingameTransactionId"] = ((booking["id"] + index) & 0x7FFFFFFF) or 1
        notification_entries.append(make_notification_entry(
            OCP_BOOKING_NOTIFICATION_KIND, guid, OCP_BOOKING_NOTIFICATION_VALUES, payload))
    commands.append(ServerCommand(NOTIFICATION_COMMAND, Notification(1, tuple(notification_entries)), 0))
    return encode_server_command_batch(tuple(commands), include_terminator=True)


class LiveAdministration:
    def __init__(self, service):
        self.service = service

    def start(self):
        if self.service.application is not None:
            for server in (self.service.application.login, self.service.application.characters):
                server.admin_commands = AdminCommandQueue(server)

    def stop(self):
        if self.service.application is not None:
            for server in (self.service.application.login, self.service.application.characters):
                controls = getattr(server, "admin_commands", None)
                if controls is not None:
                    controls.close()

    def find(self, cid):
        app = self.service.application
        if app is not None:
            for server in (app.characters, app.login):
                for s in server.session_snapshot():
                    with s.flow_lock:
                        available = ready(s, cid)
                    if available and server.is_session_current(s):
                        return server, s
        return None

    def has_receipt(self, cid, booking_id):
        app = self.service.application
        if app is not None:
            for server in (app.characters, app.login):
                for s in server.session_snapshot():
                    with s.flow_lock:
                        if (s.gameplay_character is not None and s.gameplay_character.character_id == cid
                                and booking_id in s.gameplay_admin_booking_ids):
                            return True
        return False

    def inspect(self, cid):
        target = self.find(cid)
        catalog = load_status_runtime_catalog(self.service.static_database_path)
        effects, map_id, all_maps = (), None, False
        if target:
            with target[1].flow_lock:
                effects = tuple(target[1].gameplay_persistent_status_effects.values())
                map_id = target[1].gameplay_map
                all_maps = target[1].gameplay_admin_all_maps
        return {"ready": bool(target), "map_id": map_id, "all_maps_unlocked": all_maps, "effects": [
            {"id": key, "label": preset["label"], "description": preset["description"],
             "available": preset["resource"] in catalog.effect_ids,
             "enabled": bool(matching_admin_effects(effects, catalog.effect_ids.get(preset["resource"]), preset))}
            for key, preset in ADMIN_EFFECTS.items()]}

    def action(self, cid, action, payload, actor, reason):
        target = self.find(cid)
        if target is None:
            raise ValueError("character must be online and fully loaded into a map")
        server, s = target
        with s.flow_lock:
            context = (s.gameplay_map, s.gameplay_chase_generation, id(s.gameplay_combat_state))
        if action == "effect":
            effect, enabled = payload.get("effect"), payload.get("enabled")
            if not isinstance(effect, str) or effect not in ADMIN_EFFECTS or type(enabled) is not bool:
                raise ValueError("choose a supported effect and an enabled state")
            details = {"effect": effect, "enabled": enabled}
        elif action == "unlock_maps":
            details = {"all_maps_unlocked": True}
        elif action == "teleport":
            destination = payload.get("map_id")
            if not isinstance(destination, str) or destination not in {m["map_id"] for m in map_catalog(self.service.static_database_path)}:
                raise ValueError("choose a map with an installed map database")
            difficulty = payload.get("scaling_difficulty", 0)
            if type(difficulty) is not int or not 0 <= difficulty <= 100:
                raise ValueError("scaling difficulty must be a whole number from 0 to 100")
            selection = resolve_map_access(self.service.static_database_path, destination,
                player_level=s.gameplay_character.level, scaling_difficulty=difficulty)
            details = {"map_id": destination, "physical_map": selection.physical_map_id,
                       "scaling_difficulty": selection.scaling_difficulty, "from_map": context[0]}
        else:
            raise ValueError("unknown live action")
        def execute():
            if not server.is_session_current(s):
                raise ValueError("character session changed; refresh the page")
            with s.flow_lock:
                if not ready(s, cid) or context != (s.gameplay_map, s.gameplay_chase_generation, id(s.gameplay_combat_state)):
                    raise ValueError("character changed maps; refresh the page")
            if action == "effect":
                CustomizationService._set_admin_effect(server, s, effect, enabled)
            elif action == "unlock_maps":
                MapUnlockService._unlock_all_maps(server, s)
            elif not server._begin_map_transfer(s, destination, reason="admin-cms", gameplay_switch=True,
                                               scaling_difficulty=difficulty):
                raise ValueError("a map transfer is already in progress")
            return {"ok": True, **details}
        # Record the attempt before mutation as well as the outcome afterward.
        repository = self.service.repository
        repository.audit(actor, "character." + action + ".requested", cid, {**details, "reason": reason})
        try:
            result = call_admin_command(server.admin_commands, execute, session=s)
            self.service.store.flush()
        except Exception as exc:
            repository.audit(actor, "character." + action + ".failed", cid, {**details, "reason": reason, "error": str(exc)[:500]})
            raise
        repository.audit(actor, "character." + action, cid, {**details, "reason": reason})
        return result

    def refresh_progress(self, cid, *, previous_level):
        """Run the gameplay-thread half of a CMS level change.

        The caller has already written the new level onto the session's
        character copy; this refreshes the combat maxima, tells the client
        through the same XP/skill-book commands a level-up uses, and saves.
        """
        target = self.find(cid)
        if target is None:
            return False
        server, s = target

        def execute():
            if not server.is_session_current(s):
                return False
            with s.flow_lock:
                if not ready(s, cid):
                    return False
                character = s.gameplay_character
                class_id, class_name = int(character.class_id), character.class_name
                level, xp = int(character.level), int(character.progression_u32)
                quick_slots = s.gameplay_quick_slots
            path = self.service.static_database_path
            base_xp, next_xp = player_level_xp(path, class_name, level)
            transition = PlayerXPTransition(
                awarded=0, xp=xp, level=level, level_base_xp=base_xp,
                next_level_base_xp=next_xp, level_up=level > int(previous_level))
            packet, packet_bits = encode_server_command_batch(experience_commands(
                path, class_id, transition,
                actor_id=LOCAL_PLAYER_ACTOR_ID, quick_slots=quick_slots,
            ), include_terminator=True)
            CharacterService._send_world_loot_state_batch(
                s, packet, packet_bits, label="admin-cms-level")
            if transition.level_up:
                CharacterService._refresh_player_combat_vitals(s)
                CharacterService._achievement_on_level_up(s, class_id, level)
            CharacterService._request_account_save(s, "admin-cms-level")
            return True
        return bool(call_admin_command(server.admin_commands, execute, session=s))

    def set_wisdom_level(self, cid, experience):
        """Apply a Wisdom XP threshold to a ready live character and refresh its UI."""
        target = self.find(cid)
        if target is None:
            return False
        server, s = target

        def execute():
            if not server.is_session_current(s):
                return False
            with s.flow_lock:
                if not ready(s, cid):
                    return False
                points = tuple(s.gameplay_talent_category_points)
                if len(points) != 4:
                    raise ValueError("Character has no active talent progression")
                s.gameplay_talent_category_points = (
                    int(points[0]), int(experience), int(points[2]), int(points[3]))
                updated = tuple(s.gameplay_talent_category_points)
                categories = copy.deepcopy(s.gameplay_talent_categories)
            packet, packet_bits = encode_server_command_batch((
                ServerCommand(TALENT_CATEGORY_POINTS_INFO_COMMAND,
                              TalentCategoryPointsInfo(0xFF, updated), 0),
                ServerCommand(TALENT_SELECTION_INFO_COMMAND,
                              TalentSelectionInfo(0, tuple(categories)), 0),
            ), include_terminator=True)
            CharacterService._send_world_loot_state_batch(
                s, packet, packet_bits, label="admin-cms-wisdom")
            CharacterService._request_account_save(
                s, "admin-cms-wisdom", flush=True)
            return True

        return bool(call_admin_command(server.admin_commands, execute, session=s))

    def set_honor_points(self, cid, honor_points):
        """Apply a career honor total to a ready live character and save it."""
        target = self.find(cid)
        if target is None:
            return False
        server, s = target

        def execute():
            if not server.is_session_current(s):
                return False
            with s.flow_lock:
                if not ready(s, cid):
                    return False
                s.gameplay_honor_points = int(honor_points)
            packet, packet_bits = encode_honor_points_changed(
                HonorPointsChanged(int(honor_points), LOCAL_PLAYER_ACTOR_ID)
            )
            CharacterService._send_world_loot_state_batch(
                s, packet, packet_bits, label="admin-cms-honor-points")
            CharacterService._request_account_save(
                s, "admin-cms-honor-points", flush=True)
            return True

        return bool(call_admin_command(server.admin_commands, execute, session=s))


    def deliver(self, booking_id):
        service = self.service
        # Same cancellation/delivery gate as offline initialization. Never held
        # by the game thread: it applies only the prepared detached mutation.
        with service.delivery.lock:
            booking = service.repository.booking(booking_id)
            if booking["status"] != "pending":
                return booking
            cid = booking["character_id"]
            target = self.find(cid)
            if target is None:
                return booking
            server, s = target
            try:
                owner = service.registry.character_owner(cid)
                if owner is None or owner.account_id != booking["account_id"]:
                    raise ValueError("character ownership changed")
                for _ in range(3):
                    with s.flow_lock:
                        token = state_token(s)
                    before = PlayerPersistenceService._capture_account_state(server, s)
                    if before is None:
                        raise ValueError("character state is not ready")
                    with s.flow_lock:
                        if token != state_token(s):
                            continue
                    if booking_id in before.admin_booking_ids:
                        # An earlier live commit may have survived an interrupted
                        # database acknowledgement. Save its current state only.
                        def acknowledge():
                            if not server.is_session_current(s) or not ready(s, cid):
                                raise ValueError("character session changed; acknowledgement will retry at next entry")
                            return PlayerPersistenceService._persist_gameplay_session(
                                server, s, reason="CMS booking acknowledgement")
                        call_admin_command(server.admin_commands, acknowledge, session=s)
                        service.store.flush()
                        service.repository.finish_booking(booking_id)
                        return service.repository.booking(booking_id)
                    details = {}
                    after = service.delivery._apply(before, booking, details=details)
                    packet, bits = booking_packet(after, booking, details.get("mutation"), details=details)
                    def commit():
                        if not server.is_session_current(s):
                            raise ValueError("character session changed; booking remains queued")
                        with s.flow_lock:
                            if not ready(s, cid):
                                raise ValueError("character is changing maps; booking remains queued")
                            if token != state_token(s) or getattr(s, "gameplay_inventory_rewards_in_progress", 0):
                                return False
                            if booking["kind"] in {"item", "batch"}:
                                s.gameplay_inventory = after.inventory
                                s.gameplay_next_item_id = after.next_item_id
                            if booking["kind"] == "batch":
                                s.gameplay_game_currencies = dict(after.game_currencies)
                                s.gameplay_currency_state = after.currency
                            elif booking["kind"] == "currency" and booking["resource"] in GAME_CURRENCY_BY_NAME:
                                s.gameplay_game_currencies = dict(after.game_currencies)
                            elif booking["kind"] == "currency":
                                s.gameplay_currency_state = after.currency
                            s.gameplay_admin_booking_ids = after.admin_booking_ids
                        # Publication uses the normal save path. Its receipt is
                        # captured with the balances, preventing stale autosaves.
                        PlayerPersistenceService._persist_gameplay_session(server, s, reason="CMS booking")
                        s.send_reliable_ordered_split(packet, total_bit_length=bits, track=True,
                                                     label_prefix="admin-booking")
                        return True
                    if call_admin_command(server.admin_commands, commit, session=s):
                        service.store.flush()
                        service.repository.finish_booking(booking_id)
                        return service.repository.booking(booking_id)
                raise ValueError("character inventory changed during delivery; retry when ready")
            except (ValueError, OSError, RuntimeError) as exc:
                # Once committed, the durable/live receipt forbids a second
                # credit even if sending or acknowledgement fails.
                service.repository.booking_error(booking_id, str(exc))
            return service.repository.booking(booking_id)
