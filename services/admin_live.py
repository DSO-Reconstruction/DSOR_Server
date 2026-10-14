"""CMS bridge to current gameplay sessions, native rewards and transfers."""

import copy
from contextlib import closing
from dataclasses import replace
from functools import lru_cache
import sqlite3

from dso_server.gameplay.local_map_state import map_database_path
from dso_server.gameplay.map_difficulty_state import resolve_map_access
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.protocol.currency_protocol import GAME_CURRENCY_BY_NAME, GAME_CURRENCY_CHANGED_COMMAND, GameCurrencyChanged
from dso_server.protocol.inventory_codec import INVENTORY_INFO_COMMAND
from dso_server.protocol.world_loot_protocol import EQUIPMENT_RARITIES
from dso_server.protocol.player_init_protocol import (
    CURRENCY_CHANGED_COMMAND, ServerCommand, Notification, make_notification_entry, encode_server_command_batch,
)
from dso_server.protocol.reward_protocol import (
    NOTIFICATION_COMMAND, OCP_BOOKING_NOTIFICATION_KIND, OCP_BOOKING_NOTIFICATION_VALUES,
    OCPBookingReward, ocp_booking_notification_payload,
)
from dso_server.runtime.admin_commands import AdminCommandQueue
from dso_server.services.admin_effects import ADMIN_EFFECTS, matching_admin_effects
from dso_server.services.customization import CustomizationService
from dso_server.services.map_unlocks import MapUnlockService
from dso_server.services.player_persistence import PlayerPersistenceService


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


def booking_packet(after, booking, mutation):
    commands = []
    resource = booking["resource"]
    if booking["kind"] == "item":
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
        template = resource
    elif resource in GAME_CURRENCY_BY_NAME:
        template = GAME_CURRENCY_BY_NAME[resource].wire_id
        commands.append(ServerCommand(GAME_CURRENCY_CHANGED_COMMAND,
                                       GameCurrencyChanged(template, after.game_currencies[template]), 1))
    else:
        template = _CURRENCY_TEMPLATES[resource]
        commands.append(ServerCommand(CURRENCY_CHANGED_COMMAND, replace(after.currency, notify=True), 1))
    guid = BOOKING_GUID_PREFIX + booking["id"].to_bytes(8, "big")
    rarity = EQUIPMENT_RARITIES[(booking.get("options") or {}).get("rarity", 0)]
    payload = ocp_booking_notification_payload(OCPBookingReward("cms", template, booking["amount"], rarity), guid)
    # The grant has already been committed. Acknowledging this notice must
    # never trigger another credit or require the player to accept a reward.
    payload["ingameTransactionId"] = (booking["id"] & 0x7FFFFFFF) or 1
    entry = make_notification_entry(OCP_BOOKING_NOTIFICATION_KIND, guid, OCP_BOOKING_NOTIFICATION_VALUES, payload)
    commands.append(ServerCommand(NOTIFICATION_COMMAND, Notification(1, (entry,)), 0))
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
            result = server.admin_commands.call(execute)
            self.service.store.flush()
        except Exception as exc:
            repository.audit(actor, "character." + action + ".failed", cid, {**details, "reason": reason, "error": str(exc)[:500]})
            raise
        repository.audit(actor, "character." + action, cid, {**details, "reason": reason})
        return result

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
                        server.admin_commands.call(acknowledge)
                        service.store.flush()
                        service.repository.finish_booking(booking_id)
                        return service.repository.booking(booking_id)
                    details = {}
                    after = service.delivery._apply(before, booking, details=details)
                    packet, bits = booking_packet(after, booking, details.get("mutation"))
                    def commit():
                        if not server.is_session_current(s):
                            raise ValueError("character session changed; booking remains queued")
                        with s.flow_lock:
                            if not ready(s, cid):
                                raise ValueError("character is changing maps; booking remains queued")
                            if token != state_token(s) or getattr(s, "gameplay_inventory_rewards_in_progress", 0):
                                return False
                            if booking["kind"] == "item":
                                s.gameplay_inventory = after.inventory
                                s.gameplay_next_item_id = after.next_item_id
                            elif booking["resource"] in GAME_CURRENCY_BY_NAME:
                                s.gameplay_game_currencies = dict(after.game_currencies)
                            else:
                                s.gameplay_currency_state = after.currency
                            s.gameplay_admin_booking_ids = after.admin_booking_ids
                        # Publication uses the normal save path. Its receipt is
                        # captured with the balances, preventing stale autosaves.
                        PlayerPersistenceService._persist_gameplay_session(server, s, reason="CMS booking")
                        s.send_reliable_ordered_split(packet, total_bit_length=bits, track=True,
                                                     label_prefix="admin-booking")
                        return True
                    if server.admin_commands.call(commit):
                        service.store.flush()
                        service.repository.finish_booking(booking_id)
                        return service.repository.booking(booking_id)
                raise ValueError("character inventory changed during delivery; retry when ready")
            except (ValueError, OSError, RuntimeError) as exc:
                # Once committed, the durable/live receipt forbids a second
                # credit even if sending or acknowledgement fails.
                service.repository.booking_error(booking_id, str(exc))
            return service.repository.booking(booking_id)
