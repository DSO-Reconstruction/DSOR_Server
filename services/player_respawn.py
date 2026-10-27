"""Authoritative death dialog and voluntary respawn outside battlegrounds."""

from datetime import datetime, timezone
from functools import lru_cache
import sqlite3

from dso_server.protocol.membership_protocol import parse_membership_expiry
from dso_server.persistence.readonly_db import connect
from dso_server.gameplay.local_map_state import _transform
from dso_server.protocol.respawn_protocol import (
    decode_respawn,
    encode_death_dialog,
    encode_revive,
)
from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


# The captured Kill at 01:48:53.557 is followed by the dialog at
# 01:48:55.872, roughly 58 server ticks later. Kill.animation_time is
# corpse lifetime (24 hours), not the duration of the death animation.
_DEATH_DIALOG_DELAY_SECONDS = 58 / SERVER_TICKS_PER_SECOND
_DEATH_DIALOG_TIMER_KEY = ("player-death-dialog", 0)


class _InsufficientRespawnFunds(ValueError):
    pass


@lru_cache(maxsize=512)
def respawn_map_allowed(database_path: str, map_id: str) -> bool:
    # Match settings take priority over the generic arena template. The Royal
    # Arena (a0201_basicarena) is OpenPVE and supports ordinary PvP duels, so
    # players there need the normal lethal-damage and respawn flow. Other
    # basic arenas remain governed by their AreaType below.
    if battleground_ruleset(map_id) is not None:
        return False
    with connect(database_path) as database:
        if database.execute(
            "SELECT 1 FROM _Template_BattlegroundSettings WHERE MapName=? LIMIT 1",
            (map_id,),
        ).fetchone():
            return False
        row = database.execute(
            "SELECT AreaType FROM _Template_MapTemplate WHERE MapTemplate=? LIMIT 1",
            (map_id,),
        ).fetchone()
    return row is not None and row[0] in {"OpenPVE", "ClosedPVE", "Boss"}


@lru_cache(maxsize=512)
def _is_open_pve_map(database_path: str, map_id: str) -> bool:
    with connect(database_path) as database:
        row = database.execute(
            "SELECT AreaType FROM _Template_MapTemplate "
            "WHERE MapTemplate=? LIMIT 1",
            (map_id,),
        ).fetchone()
    return row is not None and str(row[0] or "") == "OpenPVE"


@lru_cache(maxsize=8)
def _respawn_prices(database_path: str) -> tuple[int, int]:
    with connect(database_path) as database:
        row = database.execute(
            "SELECT RespawnCost, InstantRespawnCost FROM _Globals LIMIT 1"
        ).fetchone()
    if row is None or any(value is None or int(value) < 0 for value in row):
        raise ValueError("invalid respawn prices in _Globals")
    return tuple(int(value) for value in row)


@lru_cache(maxsize=8)
def _effects_removed_on_death(database_path: str) -> frozenset[int]:
    with connect(database_path) as database:
        return frozenset(int(row[0]) for row in database.execute(
            "SELECT rowid - 1 FROM _Template_StatusEffect WHERE COALESCE(KeepOnDeath, 0)=0"
        ))


def _premium_respawn(s: Session) -> bool:
    if not s.gameplay_membership_active or s.gameplay_membership_type not in (1, 2):
        return False
    try:
        return parse_membership_expiry(s.gameplay_membership_expires_at) > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


def _nearest_exit_transform(database_path, map_id, exits, death_position):
    def distance(position, origin):
        return sum((float(a) - float(b)) ** 2 for a, b in zip(position, origin))

    # If the map has a live travel stone, use its authored respawn point when
    # one is nearby. This keeps the player close to the stone without placing
    # them inside the travel object. Some maps have no nearby Respawn entry;
    # in that case place them a few metres in front of the stone.
    map_path = map_database_path(database_path, map_id)
    with connect(map_path) as database:
        travel_points = tuple(
            bytes(row[0])
            for row in database.execute(
                "SELECT Transform FROM _Instance_TravelPoint "
                "WHERE lower(COALESCE(State, 'active'))='active' "
                "AND Transform IS NOT NULL"
            )
        )
        respawn_entries = tuple(
            _transform(bytes(row[0]))
            for row in database.execute(
                "SELECT Transform FROM _Instance_Entry "
                "WHERE EntryType='Respawn' AND Transform IS NOT NULL"
            )
        )
    if travel_points:
        decoded_points = tuple(
            _transform(transform)
            for transform in travel_points
            if len(transform) == 64
        )
        if decoded_points:
            stone_position, stone_heading = min(
                decoded_points,
                key=lambda item: distance(item[0], death_position),
            )
            if respawn_entries:
                nearby_respawn = min(
                    respawn_entries,
                    key=lambda entry: distance(entry[0], stone_position),
                )
                if distance(nearby_respawn[0], stone_position) <= 12.0:
                    return nearby_respawn
            offset = 4.0
            return (
                (
                    stone_position[0] + math.sin(stone_heading) * offset,
                    stone_position[1],
                    stone_position[2] + math.cos(stone_heading) * offset,
                ),
                stone_heading,
            )

    active_exits = tuple(exit for exit in exits if exit.active)
    nearest_position = (
        min(active_exits, key=lambda exit: distance(exit.position, death_position)).position
        if active_exits else load_local_spawn_position(database_path, map_id)
    )
    # Use an entry on the inside of the exit, not its travel trigger volume.
    with connect(map_database_path(database_path, map_id)) as database:
        entries = tuple(
            _transform(bytes(row[0])) for row in database.execute(
                "SELECT Transform FROM _Instance_Entry "
                "WHERE EntryType IN ('Entry', 'Respawn') ORDER BY rowid"
            )
        )
    if not entries:
        raise ValueError("map has no safe respawn entry")
    return min(entries, key=lambda entry: distance(entry[0], nearest_position))


class PlayerRespawnService:
    def _initialize_respawn_state(self, s: Session, persisted_state, *, map_id=None) -> None:
        map_id = str(map_id or s.gameplay_map or DEFAULT_GAMEPLAY_MAP)
        s.gameplay_respawn_allowed = respawn_map_allowed(
            str(self.static_database_path), map_id
        )
        s.gameplay_last_visited_town = str(
            getattr(persisted_state, "last_visited_town", "") or ""
        )
        if self.static_database.gameplay_map_ruleset(map_id) == 5:
            s.gameplay_last_visited_town = map_id
        CharacterService._cancel_player_death_dialog(s)
        s.gameplay_death_pending = False
        s.gameplay_respawn_in_progress = False
        s.gameplay_revive_movement_until = 0.0
        s.gameplay_revive_movement_correction_until = 0.0
        s.gameplay_respawn_protection_instance_id = None

    @staticmethod
    def _respawn_actor_id(s: Session) -> int:
        if s.gameplay_presence_callback is not None and s.gameplay_character is not None:
            return int(s.gameplay_remote_actor_id or CharacterService._remote_actor_id(
                s.gameplay_character.character_id
            ))
        return int(s.gameplay_combat_state.player_actor_id)

    @staticmethod
    def _clear_player_death_effects(s: Session) -> None:
        # Kill clears these on the client. Drop the corresponding server
        # runtime records so an old stun/DoT cannot survive the new life.
        runtime = getattr(s, "gameplay_runtime_status_effects", {})
        victims = getattr(s, "gameplay_victim_status_effect_instances", {})
        persistent = getattr(s, "gameplay_persistent_status_effects", {})
        if not runtime and not victims and not persistent:
            return
        database_path = getattr(
            getattr(s.gameplay_combat_state, "encounter", None),
            "static_database_path", DEFAULT_STATIC_DB_PATH,
        )
        removed_ids = _effects_removed_on_death(str(database_path))
        from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
        effect_groups = load_status_runtime_catalog(database_path).groups
        timers = []
        changed = False
        removed_quest_reward = False
        with s.flow_lock:
            for instance_id, entry in tuple(persistent.items()):
                if (entry.effect_id not in removed_ids
                        or "questreward" not in effect_groups.get(entry.effect_id, ())):
                    continue
                persistent.pop(instance_id, None)
                s.gameplay_timed_status_effect_expiries.pop(instance_id, None)
                for prefix in ("consumable-expire", "consumable-health-tick", "consumable-xp-tick"):
                    timer = s.gameplay_projectile_timers.pop((prefix, instance_id), None)
                    if timer is not None:
                        timers.append(timer)
                changed = removed_quest_reward = True
            for instance_id, (entry, _expiry) in tuple(runtime.items()):
                if int(entry.effect_id) in removed_ids:
                    runtime.pop(instance_id, None)
                    getattr(s, "gameplay_item_set_programs", {}).pop(instance_id, None)
                    program_timer = s.gameplay_projectile_timers.pop(("item-set-program-expire", int(instance_id)), None)
                    if program_timer is not None:
                        timers.append(program_timer)
                    changed = True
                    timer = s.gameplay_projectile_timers.pop(
                        ("player-status-tick", int(instance_id)), None
                    )
                    if timer is not None:
                        timers.append(timer)
            for key, (entry, _expiry) in tuple(victims.items()):
                if int(key[0]) == LOCAL_PLAYER_ACTOR_ID and int(entry.effect_id) in removed_ids:
                    victims.pop(key, None)
                    changed = True
        for timer in timers:
            timer.cancel()
        if removed_quest_reward:
            CharacterService._request_account_save(s, "quest status rewards", flush=True)
        if changed:
            CharacterService._refresh_player_combat_vitals(s)

    @staticmethod
    def _notify_player_death(s: Session, *, expected_generation=None) -> None:
        with s.flow_lock:
            if expected_generation is not None and expected_generation != s.gameplay_chase_generation:
                return
            if getattr(s, "gameplay_death_pending", False):
                return
            s.gameplay_death_pending = True
            if s.gameplay_inventory is not None:
                s.gameplay_inventory.health_points = 0.0
            s.gameplay_respawn_protection_instance_id = None
            allowed = getattr(s, "gameplay_respawn_allowed", False)
        CharacterService._clear_player_death_effects(s)
        if allowed and getattr(s, "gameplay_match", None) is None:
            with s.flow_lock:
                if not s.gameplay_death_pending or s.gameplay_respawn_in_progress:
                    return
                generation = s.gameplay_chase_generation
                timer = s.gameplay_deadline_timer(
                    _DEATH_DIALOG_DELAY_SECONDS,
                    lambda: CharacterService._show_player_death_dialog(s, generation, timer),
                )
                previous = s.gameplay_projectile_timers.get(_DEATH_DIALOG_TIMER_KEY)
                s.gameplay_projectile_timers[_DEATH_DIALOG_TIMER_KEY] = timer
            if previous is not None:
                previous.cancel()
            timer.start()

    @staticmethod
    def _cancel_player_death_dialog(s: Session) -> None:
        with s.flow_lock:
            timer = s.gameplay_projectile_timers.pop(_DEATH_DIALOG_TIMER_KEY, None)
        if timer is not None:
            timer.cancel()

    @staticmethod
    def _show_player_death_dialog(s: Session, generation: int, timer) -> None:
        with s.flow_lock:
            # Identity prevents a cancelled callback from touching the timer
            # for a later death, including another death in the same map.
            if s.gameplay_projectile_timers.get(_DEATH_DIALOG_TIMER_KEY) is not timer:
                return
            s.gameplay_projectile_timers.pop(_DEATH_DIALOG_TIMER_KEY)
            if (not s.connected or generation != s.gameplay_chase_generation
                    or not s.gameplay_death_pending or s.gameplay_respawn_in_progress
                    or s.gameplay_exit_transition_started or s.gameplay_presence_departed
                    or not s.gameplay_respawn_allowed or s.gameplay_match is not None):
                return
        # The real transport forbids sending while holding mutable state
        # locks. Keep validation/claim above separate from packet delivery.
        s.send_reliable_ordered(
            encode_death_dialog(), track=True, label="player-death-dialog"
        )

    @staticmethod
    def _send_player_revive_to(s: Session, *, actor_id, position, heading, health, resource):
        s.send_reliable_ordered(
            encode_force_transform_batch(ForceTransform(position, heading), actor_id=actor_id),
            track=True, label="player-respawn-transform",
        )
        s.send_reliable_ordered(
            encode_actor_stats_update_batch(ActorStatsUpdate(health, resource), actor_id=actor_id),
            track=True, label="player-respawn-vitals",
        )
        s.send_reliable_ordered(
            encode_revive(actor_id=actor_id), track=True, label="player-revive"
        )

    def _handle_respawn(self, s: Session, payload: bytes) -> None:
        try:
            request = decode_respawn(payload, getattr(s, "current_message_bit_length", None))
            allowed = respawn_map_allowed(str(self.static_database_path), str(s.gameplay_map))
        except (ValueError, OSError, sqlite3.Error) as exc:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected RespawnCommand: {exc}")
            return
        if (getattr(s, "gameplay_match", None) is not None
                and getattr(s, "gameplay_match_callback", None) is not None):
            # The PvP death dialog's Respawn button sends this command too
            # (PVPDeathDialog 0x140302b14). Battleground deaths are resolved
            # by the match, never by the open-world respawn options.
            s.gameplay_match_callback(s, ("match_respawn_request", request))
            return
        with s.flow_lock:
            combat = s.gameplay_combat_state
            if (not allowed or s.gameplay_match is not None or not s.connected
                    or not s.gameplay_presence_ready
                    or s.gameplay_exit_transition_started or s.gameplay_presence_departed
                    or s.gameplay_respawn_in_progress or combat is None
                    or s.gameplay_character is None or s.gameplay_inventory is None):
                return
            s.gameplay_respawn_in_progress = True
        committed = False
        revival_unlocked = False
        revived_pool = None
        try:
            actor_id = CharacterService._respawn_actor_id(s)
            with combat.lock:
                health = combat.player_health.get(actor_id)
                if health is None or int(health[0]) > 0:
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] rejected RespawnCommand: "
                        f"actor {actor_id} is not dead (health={health})"
                    )
                    return
            CharacterService._cancel_player_death_dialog(s)
            if not request.nearest_exit and not request.same_spot:
                town = s.gameplay_last_visited_town or self._nearest_login_hub(s.gameplay_map)
                if self.static_database.gameplay_map_ruleset(town) != 5:
                    raise ValueError("last visited town is not a hub")
                self._begin_map_transfer(
                    s, town, spawn_position=load_local_spawn_position(self.static_database_path, town),
                    reason="respawn-town", gameplay_switch=True,
                )
                return

            with s.flow_lock:
                move = s.gameplay_last_move
                frame = s.gameplay_coordinate_frame or combat.encounter.coordinate_frame
                position = (frame.wire_to_world(move.relative_position) if move is not None
                            else s.gameplay_spawn_position)
                heading = move.yaw if move is not None else 0.0
                tick = int(s.gameplay_chase_server_tick)
            if position is None:
                raise ValueError("dead player has no authoritative position")
            if request.nearest_exit:
                position, heading = _nearest_exit_transform(
                    self.static_database_path, s.gameplay_map, s.gameplay_map_exits, position
                )
            respawn_move = Move.from_values(
                frame.world_to_wire(position), 0.0, heading, heading, tick, 0
            )
            premium = _premium_respawn(s)
            prices = _respawn_prices(str(self.static_database_path))
            cost = 0 if premium else prices[1 if request.same_spot else 0]
            with connect(self.static_database_path) as database:
                # Retail uses different animation/protection pairs for each
                # destination, even when the player pays for the revival.
                effects = LocalCombatState._load_victim_status_effects(
                    database,
                    "resurrect_self,$0:0;InvulnerablePremiumRespawn"
                    if request.same_spot else "map_respawn;Invulnerable",
                )
            if len(effects) != 2:
                raise ValueError("missing revival animation or protection effect")
            pvp_open_world = (
                bool(getattr(s, "gameplay_pvp_enabled", False))
                and _is_open_pve_map(
                    str(self.static_database_path), str(s.gameplay_map)
                )
            )
            if pvp_open_world:
                effects = (
                    effects[0],
                    replace(
                        effects[1],
                        duration_ticks=5 * SERVER_TICKS_PER_SECOND,
                    ),
                )
            revival_seconds = effects[0].duration_ticks / SERVER_TICKS_PER_SECOND
            # Session and map locks must never overlap. Keep the corpse
            # untargetable while restoring its map-owned pool, then charge
            # and publish together. A handoff before publication rolls back
            # the unannounced revival without exposing a provisional charge
            # to the source map's persistence snapshot.
            with s.flow_lock:
                currency = s.gameplay_currency_state
                inventory = s.gameplay_inventory
                generation = s.gameplay_chase_generation
                if (s.gameplay_combat_state is not combat or not s.connected
                        or s.gameplay_exit_transition_started
                        or s.gameplay_presence_departed or currency is None
                        or inventory is None):
                    return
                if int(currency.currency_type) < cost:
                    raise _InsufficientRespawnFunds()
                s.gameplay_death_pending = True
            with combat.lock:
                health = combat.player_health.get(actor_id)
                if (health is None or int(health[0]) > 0
                        or s.gameplay_combat_state is not combat
                        or s.gameplay_chase_generation != generation
                        or not s.connected or s.gameplay_exit_transition_started
                        or s.gameplay_presence_departed):
                    return
                revived_pool = health
                health[0] = current_health = max(1, int(health[1]))
                resource = float(combat.player_resource.get(actor_id, [0.0])[0])
                if actor_id == combat.player_actor_id:
                    combat.player_current_health = current_health
            with s.flow_lock:
                if (s.gameplay_combat_state is not combat
                        or s.gameplay_chase_generation != generation
                        or s.gameplay_inventory is not inventory
                        or not s.connected or s.gameplay_exit_transition_started
                        or s.gameplay_presence_departed):
                    return
                currency = s.gameplay_currency_state
                if currency is None:
                    return
                if int(currency.currency_type) < cost:
                    raise _InsufficientRespawnFunds()
                currency = replace(currency, currency_type=int(currency.currency_type) - cost)
                cost_packet = encode_server_command_batch((
                    ServerCommand(
                        UPDATE_PROGRESS_TIER_SPENDER_CHALLENGES_COMMAND,
                        UpdateProgressTierSpenderChallenges(cost), 0,
                    ),
                    ServerCommand(
                        CURRENCY_CHANGED_COMMAND, currency, LOCAL_PLAYER_ACTOR_ID,
                    ),
                )) if cost else None
                s.gameplay_currency_state = currency
                inventory.health_points = float(current_health)
                inventory.secondary_resource = resource
                s.gameplay_revive_movement_until = time.perf_counter() + revival_seconds
                s.gameplay_revive_movement_correction_until = 0.0
                s.gameplay_spawn_position = tuple(position)
                s.gameplay_last_move = respawn_move
                s.gameplay_move_budget = 0.0
                s.gameplay_movement_allowance = None
                s.gameplay_move_correction_at = 0.0
                s.gameplay_exit_inside_guids.clear()
                s.gameplay_exit_distances.clear()
                committed = True
            if cost:
                s.send_reliable_ordered(cost_packet, track=True, label="respawn-cost")
                self._credit_tier_spender_progress(s, cost)
            CharacterService._send_player_revive_to(
                s, actor_id=LOCAL_PLAYER_ACTOR_ID, position=position,
                heading=heading, health=current_health, resource=resource,
            )
            if s.gameplay_presence_callback is not None:
                s.gameplay_presence_callback(s, (
                    "player_revived", position, heading, current_health, resource,
                ))
            CharacterService._send_player_skill_status_effects(
                s, effects, server_tick=tick, label="player-revive-effects"
            )
            if pvp_open_world:
                protection_effect_id = int(effects[1].effect_id)
                with s.flow_lock:
                    protection_ids = tuple(
                        int(instance_id)
                        for instance_id, (entry, _expiry)
                        in s.gameplay_runtime_status_effects.items()
                        if int(entry.effect_id) == protection_effect_id
                    )
                    s.gameplay_respawn_protection_instance_id = (
                        max(protection_ids) if protection_ids else None
                    )
            with s.flow_lock:
                s.gameplay_death_pending = False
                revival_unlocked = True
            if request.same_spot:
                CharacterService._dispatch_socket_event(
                    s, "revive120", server_tick=tick, event_seed=tick
                )
            CharacterService._update_low_health_socket_state(
                s, current_health=current_health, maximum_health=current_health,
                server_tick=tick, event_seed=tick,
            )
            if request.same_spot:
                self._season_pass_feed(s, "RespawnInstantly")
            CharacterService._accept_gameplay_state_command(s, 0x0062)
        except _InsufficientRespawnFunds:
            s.send_reliable_ordered(
                encode_death_dialog(), track=True, label="respawn-insufficient-funds"
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            action = "respawn delivery failed" if committed else "rejected respawn"
            print(f"[{GAMEPLAY_SERVICE_NAME}] {action}: {exc}")
        finally:
            if not committed and revived_pool is not None:
                with combat.lock:
                    if combat.player_health.get(actor_id) is revived_pool:
                        revived_pool[0] = 0
                        if actor_id == combat.player_actor_id:
                            combat.player_current_health = 0
            with s.flow_lock:
                if committed and not revival_unlocked:
                    s.gameplay_death_pending = False
                s.gameplay_respawn_in_progress = False
            if committed:
                CharacterService._persist_gameplay_session(
                    self, s, reason="player respawn", flush=True
                )
