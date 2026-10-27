"""Seasonal activity lifecycles: egg incubation, wishing well, anniversary
cake milestones, and returnee/recall sign-in.

Wire layouts and client behaviour are documented in
``protocol/seasonal_activity_protocol.py``.  Per-character progress is kept
in the save's claim map (``gameplay_timed_offer_purchases``, the activity
sign-in pattern), so it is persisted with the same inventory snapshot that
pays or delivers it; server-wide totals and recall links live in
``persistence/seasonal_state.py``.

Every mutation validates first, builds the whole response batch, then
commits session state, saves (flush), and only then publishes.  Replaying a
request finds the committed state and is rejected, so nothing is granted
twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import random
import sqlite3
from typing import Any

from dso_server.persistence.seasonal_state import (
    RecallLinkConflict,
    RecallLinkLimit,
    SeasonalStateError,
    default_seasonal_state_path,
    shared_seasonal_store,
)
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.command_ids import UNAVAILABLE_COMMAND_ID
from dso_server.protocol.commerce_protocol import load_purchase_item_definition
from dso_server.protocol.currency_protocol import GAME_CURRENCY_CHANGED_COMMAND
from dso_server.protocol.gameplay_commands import LOCAL_PLAYER_ACTOR_ID
from dso_server.protocol.gameplay_protocol import (
    MESSAGE_COMMAND,
    encode_screen_message,
)
from dso_server.protocol.item_receipt_protocol import item_received_message
from dso_server.protocol.player_init_protocol import (
    CURRENCY_CHANGED_COMMAND,
    INVENTORY_INFO_COMMAND,
    USER_HATCH_EGGS_INFO_COMMAND,
    ServerCommand,
    UserHatchEggsInfo,
    encode_server_command_batch,
)
from dso_server.protocol.seasonal_activity_protocol import (
    CAMEBACK_2024,
    CAMEBACK_2026,
    CAMEBACK_BIND_USER_2026_COMMAND,
    CAMEBACK_MODE_DAILY,
    CAMEBACK_MODE_DOUBLE,
    GET_CAMEBACK_SIGNIN_AWARD_2026_COMMAND,
    GET_CAMEBACK_SIGNIN_AWARD_COMMAND,
    GET_CAMEBACK_SIGNIN_INFO_2026_COMMAND,
    GET_CAMEBACK_SIGNIN_INFO_COMMAND,
    GET_CAMEBACK_SIGNIN_INFO_RESULT_2026_COMMAND,
    GET_CAMEBACK_SIGNIN_INFO_RESULT_COMMAND,
    GET_CAMEBACK_USER_BIND_OR_POINTS_AWARD_2026_COMMAND,
    GET_CAMEBACK_USER_INFO_2026_COMMAND,
    GET_CAMEBACK_USER_INFO_RESULT_2026_COMMAND,
    GET_CRAFTING_CAKES_AWARD_COMMAND,
    GET_CRAFTING_CAKES_INFO_COMMAND,
    GET_CRAFTING_CAKES_INFO_RESULT_COMMAND,
    HATCH_CHANCE_SCALE,
    HATCH_OPERATION_FEED,
    HATCH_OPERATION_HATCH,
    HATCH_OPERATION_START,
    OPERATE_HATCH_EGGS_COMMAND,
    RECALL_CLAIM_COINS,
    RECALL_CLAIM_LINK_REWARD,
    RECALL_CODE_INVALID_MESSAGE,
    RECALL_CODE_NOT_FOUND_MESSAGE,
    RECALL_INACTIVITY_SECONDS,
    RECALL_LINK_CONFLICT_MESSAGE,
    RECALL_LINK_LIMIT,
    RECALL_LINK_LIMIT_MESSAGE,
    RECALL_SUMMONER_SHARE_DIVISOR,
    RECALL_USER_RETURNER,
    RECALL_USER_SUMMONER,
    RETURNED_COIN_TEMPLATE,
    SELECT_WISHING_WELL_MSG_COMMAND,
    CameBackSigninAwardRequest,
    CameBackSigninInfoResult,
    CameBackUserInfo2026,
    CraftingCakesAwardRequest,
    CraftingCakesInfoResult,
    LootAward,
    OperateHatchEggsRequest,
    RecallBindOrPointsRequest,
    RecallBindUserRequest,
    RecallLinkError,
    SeasonalActivityError,
    SelectWishingWellRequest,
    apply_reward_mutation,
    date_int,
    decode_cameback_signin_award,
    decode_crafting_cakes_award,
    decode_operate_hatch_eggs,
    decode_recall_bind_or_points,
    decode_recall_bind_user,
    decode_seasonal_info_request,
    decode_select_wishing_well,
    hatch_eggs_info,
    load_cameback_campaign,
    load_crafting_cakes_definition,
    load_hatch_egg_definitions,
    load_recall_award_groups,
    load_wishing_well_definitions,
    parse_loot_awards,
    parse_recall_code,
    recall_code_for,
    resolve_offer_group,
)
from dso_server.runtime.context import GAMEPLAY_SERVICE_NAME
from dso_server.runtime.transport import Session
from dso_server.services.base import CharacterService


# Claim-map keys (``AccountState.timed_offer_purchases``; values are ints).
HATCH_DEADLINE_KEY = "hatch-eggs:{egg}:deadline"   # epoch s of the un-fed deadline
HATCH_FEEDS_KEY = "hatch-eggs:{egg}:feeds"
WISHING_WELL_KEY = "wishing-well:{template}"       # wishes made (audit count)
CAKE_CLAIM_KEY = "crafting-cakes:{recipe}:{milestone}"
CAMEBACK_DAYS_KEY = "cameback-signin:{event}:days"
CAMEBACK_LAST_KEY = "cameback-signin:{event}:last"  # YYYYMMDD
CAMEBACK_BONUS_KEY = "cameback-signin:{event}:bonus:{day}"
RECALL_RETURNER_KEY = "recall-2026:returner"        # YYYYMMDD the return was seen
RECALL_LINK_REWARD_KEY = "recall-2026:link-reward"
RECALL_INVITE_REWARD_KEY = "recall-2026:invite-reward"
RECALL_COINS_CLAIMED_KEY = "recall-2026:coins-claimed"
RECALL_LINKED_COINS_KEY = "recall-2026:linked-coins"

_CAMEBACK_COMMANDS = {
    GET_CAMEBACK_SIGNIN_INFO_COMMAND: (
        CAMEBACK_2024, GET_CAMEBACK_SIGNIN_INFO_RESULT_COMMAND,
    ),
    GET_CAMEBACK_SIGNIN_AWARD_COMMAND: (
        CAMEBACK_2024, GET_CAMEBACK_SIGNIN_INFO_RESULT_COMMAND,
    ),
    GET_CAMEBACK_SIGNIN_INFO_2026_COMMAND: (
        CAMEBACK_2026, GET_CAMEBACK_SIGNIN_INFO_RESULT_2026_COMMAND,
    ),
    GET_CAMEBACK_SIGNIN_AWARD_2026_COMMAND: (
        CAMEBACK_2026, GET_CAMEBACK_SIGNIN_INFO_RESULT_2026_COMMAND,
    ),
}
_CAMEBACK_RESULTS = {
    CAMEBACK_2024: GET_CAMEBACK_SIGNIN_INFO_RESULT_COMMAND,
    CAMEBACK_2026: GET_CAMEBACK_SIGNIN_INFO_RESULT_2026_COMMAND,
}


@dataclass(frozen=True)
class _SeasonalSnapshot:
    character: Any
    character_id: int
    level: int
    class_name: str
    inventory: Any
    currency: Any
    game_currencies: dict
    next_item_id: int
    claims: dict
    map_difficulty: int


def _claim(claims: dict, key: str) -> int:
    try:
        return int(claims.get(key, 0))
    except (TypeError, ValueError):
        return 0


def _parse_saved_at(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _hook_now(service) -> datetime:
    """Clock for hooks that other service objects reach via the facade.

    Initialization and crafting call the hooks through ``CharacterService``
    with whichever service object runs them (the legacy 2190 route is not
    composed with this mixin), so instance helpers are looked up softly.
    """

    clock = getattr(service, "_seasonal_now", None)
    return clock() if clock is not None else datetime.now(timezone.utc)


def _hook_active_event(service, template_id: str, now: datetime):
    finder = getattr(service, "_seasonal_active_event", None)
    if finder is None:
        return SeasonalActivityService._seasonal_active_event(
            service, template_id, now,
        )
    return finder(template_id, now)


class SeasonalActivityService:

    # -- environment -------------------------------------------------------

    def _seasonal_now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _seasonal_random(self) -> random.Random:
        return random.SystemRandom()

    def _seasonal_store(self):
        store = getattr(self, "seasonal_store", None)
        if store is None:
            path = default_seasonal_state_path(
                getattr(self, "account_store", None)
            )
            if path is None:
                return None
            store = shared_seasonal_store(path)
            self.seasonal_store = store
        return store

    def _seasonal_active_event(self, template_id: str, now: datetime):
        """The active scheduled event of ``template_id`` (data/events.json)."""

        key = str(template_id or "").casefold()
        if not key:
            return None
        for event in CharacterService._event_catalog(self).active_events(now):
            if event.template.resource_id.casefold() == key:
                return event
        return None

    def _seasonal_event_allows(
            self, template_id: str, now: datetime, level: int) -> bool:
        event = self._seasonal_active_event(template_id, now)
        if event is None:
            return False
        template = getattr(event, "template", None)
        minimum = int(getattr(template, "minimum_level", 0) or 0)
        maximum = int(getattr(template, "maximum_level", 0) or 0) or 10_000
        return minimum <= int(level) <= maximum

    def _seasonal_snapshot(self, s: Session) -> _SeasonalSnapshot:
        with s.flow_lock:
            character = s.gameplay_character
            inventory = s.gameplay_inventory
            currency = s.gameplay_currency_state
            if character is None or inventory is None or currency is None:
                raise SeasonalActivityError("player state is not initialized")
            return _SeasonalSnapshot(
                character=character,
                character_id=int(getattr(character, "character_id", 0)),
                level=max(1, int(getattr(character, "level", 1))),
                class_name=CLASS_NAMES.get(
                    int(getattr(character, "class_id", 0)), "warrior"
                ),
                inventory=inventory,
                currency=currency,
                game_currencies=dict(s.gameplay_game_currencies),
                next_item_id=int(s.gameplay_next_item_id),
                claims=dict(s.gameplay_timed_offer_purchases),
                map_difficulty=int(getattr(s, "gameplay_map_difficulty", 0)),
            )

    def _seasonal_mutation(self, snapshot: _SeasonalSnapshot, **changes):
        return apply_reward_mutation(
            self.static_database_path,
            snapshot.inventory,
            snapshot.currency,
            snapshot.game_currencies,
            next_item_id=snapshot.next_item_id,
            player_level=snapshot.level,
            class_name=snapshot.class_name,
            map_difficulty=snapshot.map_difficulty,
            **changes,
        )

    def _seasonal_publish(
            self, s: Session, snapshot: _SeasonalSnapshot | None, *,
            mutation=None, claims: dict | None = None,
            results: tuple = (), label: str, reason: str = "") -> None:
        """Encode, commit, save, then send one response batch."""

        commands = []
        if mutation is not None:
            commands.append(ServerCommand(
                INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID,
            ))
            if mutation.currency_changed:
                commands.append(ServerCommand(
                    CURRENCY_CHANGED_COMMAND, mutation.currency,
                    LOCAL_PLAYER_ACTOR_ID,
                ))
            commands.extend(
                ServerCommand(
                    GAME_CURRENCY_CHANGED_COMMAND, update,
                    LOCAL_PLAYER_ACTOR_ID,
                )
                for update in mutation.game_currency_updates
            )
            commands.extend(
                ServerCommand(
                    MESSAGE_COMMAND, item_received_message(receipt),
                    LOCAL_PLAYER_ACTOR_ID,
                )
                for receipt in mutation.receipts
            )
            commands.append(ServerCommand(
                INVENTORY_INFO_COMMAND, mutation.final, LOCAL_PLAYER_ACTOR_ID,
            ))
        for command, value in results:
            if command == UNAVAILABLE_COMMAND_ID:
                # The selected client build has no such result command.
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] {label}: result is not "
                    "available for this client build"
                )
                continue
            commands.append(ServerCommand(command, value, LOCAL_PLAYER_ACTOR_ID))
        packet = None
        if commands:
            packet, bits = encode_server_command_batch(
                tuple(commands), include_terminator=True,
            )
        changed = mutation is not None or claims is not None
        gains = {}
        if changed:
            if snapshot is None:
                raise SeasonalActivityError("a mutation needs its snapshot")
            with s.flow_lock:
                if (
                    s.gameplay_inventory is not snapshot.inventory
                    or s.gameplay_currency_state != snapshot.currency
                    or dict(s.gameplay_game_currencies)
                    != snapshot.game_currencies
                    or int(s.gameplay_next_item_id) != snapshot.next_item_id
                    or dict(s.gameplay_timed_offer_purchases) != snapshot.claims
                ):
                    raise SeasonalActivityError(
                        "player state changed before commit"
                    )
                if mutation is not None:
                    s.gameplay_inventory = mutation.inventory
                    s.gameplay_currency_state = mutation.currency
                    s.gameplay_game_currencies = dict(mutation.game_currencies)
                    s.gameplay_next_item_id = mutation.next_item_id
                    gains = {
                        currency_id: int(balance)
                        - int(snapshot.game_currencies.get(currency_id, 0))
                        for currency_id, balance
                        in mutation.game_currencies.items()
                    }
                if claims is not None:
                    s.gameplay_timed_offer_purchases = dict(claims)
            CharacterService._request_account_save(
                s, reason or label, flush=True,
            )
        deferred = []
        for currency_id, gained in gains.items():
            if gained > 0:
                CharacterService._achievement_feed(
                    self, s, "collectedgamecurrency", (currency_id,),
                    count=gained, deferred_packets=deferred,
                )
        if packet is not None:
            CharacterService._send_world_loot_state_batch(
                s, packet, bits, label=label,
            )
        for extra_packet, extra_bits, extra_label in deferred:
            CharacterService._send_world_loot_state_batch(
                s, extra_packet, extra_bits, label=extra_label,
            )

    # -- dispatch ----------------------------------------------------------

    def _handle_seasonal_activity(
            self, s: Session, payload: bytes, command_id: int) -> None:
        with s.gameplay_mutation_lock:
            try:
                SeasonalActivityService._dispatch_seasonal_activity(
                    self, s, bytes(payload), int(command_id),
                )
            except RecallLinkError as exc:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] rejected recall link: {exc}"
                )
                SeasonalActivityService._seasonal_send_message(
                    s, exc.message_key,
                )
                SeasonalActivityService._seasonal_refresh(self, s, command_id)
            except (OSError, ValueError, sqlite3.Error) as exc:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] rejected seasonal activity "
                    f"0x{int(command_id):04x}: {exc}"
                )
                SeasonalActivityService._seasonal_refresh(self, s, command_id)

    def _dispatch_seasonal_activity(
            self, s: Session, payload: bytes, command_id: int) -> None:
        if command_id == GET_CRAFTING_CAKES_INFO_COMMAND:
            decode_seasonal_info_request(payload, command_id)
            CharacterService._accept_gameplay_state_command(s, command_id)
            SeasonalActivityService._seasonal_cakes_info(self, s)
        elif command_id == GET_CRAFTING_CAKES_AWARD_COMMAND:
            request = decode_crafting_cakes_award(payload)
            CharacterService._accept_gameplay_state_command(s, command_id)
            SeasonalActivityService._seasonal_cakes_award(self, s, request)
        elif command_id in (
            GET_CAMEBACK_SIGNIN_INFO_COMMAND,
            GET_CAMEBACK_SIGNIN_INFO_2026_COMMAND,
        ):
            decode_seasonal_info_request(payload, command_id)
            CharacterService._accept_gameplay_state_command(s, command_id)
            SeasonalActivityService._seasonal_cameback_info(
                self, s, _CAMEBACK_COMMANDS[command_id][0],
            )
        elif command_id in (
            GET_CAMEBACK_SIGNIN_AWARD_COMMAND,
            GET_CAMEBACK_SIGNIN_AWARD_2026_COMMAND,
        ):
            request = decode_cameback_signin_award(payload, command_id)
            CharacterService._accept_gameplay_state_command(s, command_id)
            SeasonalActivityService._seasonal_cameback_award(
                self, s, _CAMEBACK_COMMANDS[command_id][0], request,
            )
        elif command_id == OPERATE_HATCH_EGGS_COMMAND:
            request = decode_operate_hatch_eggs(payload)
            CharacterService._accept_gameplay_state_command(s, command_id)
            SeasonalActivityService._seasonal_hatch(self, s, request)
        elif command_id == SELECT_WISHING_WELL_MSG_COMMAND:
            request = decode_select_wishing_well(payload)
            CharacterService._accept_gameplay_state_command(s, command_id)
            SeasonalActivityService._seasonal_wishing_well(self, s, request)
        elif command_id == GET_CAMEBACK_USER_INFO_2026_COMMAND:
            decode_seasonal_info_request(payload, command_id)
            CharacterService._accept_gameplay_state_command(s, command_id)
            SeasonalActivityService._seasonal_recall_info(self, s)
        elif command_id == GET_CAMEBACK_USER_BIND_OR_POINTS_AWARD_2026_COMMAND:
            request = decode_recall_bind_or_points(payload)
            CharacterService._accept_gameplay_state_command(s, command_id)
            SeasonalActivityService._seasonal_recall_claim(self, s, request)
        elif command_id == CAMEBACK_BIND_USER_2026_COMMAND:
            request = decode_recall_bind_user(payload)
            CharacterService._accept_gameplay_state_command(s, command_id)
            SeasonalActivityService._seasonal_recall_bind(self, s, request)
        else:
            raise SeasonalActivityError(
                f"command 0x{command_id:04x} is not a seasonal activity"
            )

    def _seasonal_refresh(self, s: Session, command_id: int) -> None:
        """Best-effort current state after a rejected request.

        The recall manager keeps its request buttons locked until a new
        UserInfoResult (and the 2026 sign-in until an InfoResult) arrives, so
        a rejection must still answer.
        """

        try:
            if command_id == GET_CRAFTING_CAKES_AWARD_COMMAND:
                SeasonalActivityService._seasonal_cakes_info(self, s)
            elif command_id in (
                GET_CAMEBACK_SIGNIN_AWARD_COMMAND,
                GET_CAMEBACK_SIGNIN_AWARD_2026_COMMAND,
            ):
                SeasonalActivityService._seasonal_cameback_info(
                    self, s, _CAMEBACK_COMMANDS[command_id][0],
                )
            elif command_id in (
                GET_CAMEBACK_USER_BIND_OR_POINTS_AWARD_2026_COMMAND,
                CAMEBACK_BIND_USER_2026_COMMAND,
            ):
                SeasonalActivityService._seasonal_recall_info(self, s)
            elif command_id == OPERATE_HATCH_EGGS_COMMAND:
                SeasonalActivityService._seasonal_send_hatch_info(self, s)
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] could not refresh seasonal "
                f"state after 0x{int(command_id):04x}: {exc}"
            )

    @staticmethod
    def _seasonal_send_message(s: Session, message_key: str) -> None:
        try:
            packet, bits = encode_screen_message(str(message_key))
            CharacterService._send_world_loot_state_batch(
                s, packet, bits, label="seasonal-message",
            )
        except (OSError, ValueError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] could not send seasonal message "
                f"{message_key!r}: {exc}"
            )

    # -- initialization hooks ----------------------------------------------

    def _seasonal_initialization(
            self, s: Session, persisted_state) -> UserHatchEggsInfo:
        """Bootstrap UserHatchEggsInfo; note a 2026 recall returner.

        Runs while the initialization batch is built, after the save's claim
        map was applied to the session and before the initialization save, so
        the previous session's ``saved_at`` is still the last activity stamp.
        """

        # Initialization must never fail because of a seasonal activity.
        try:
            SeasonalActivityService._seasonal_mark_returner(
                self, s, persisted_state, _hook_now(self),
            )
        except Exception as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] recall returner check failed: {exc}"
            )
        with s.flow_lock:
            claims = dict(s.gameplay_timed_offer_purchases)
        try:
            return SeasonalActivityService._seasonal_hatch_info(self, claims)
        except Exception as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] hatch egg bootstrap failed: {exc}"
            )
            return UserHatchEggsInfo((), ())

    def _seasonal_send_initial_hatch_info(self, s: Session) -> None:
        """Republish persisted incubations once the client is ready.

        The client's own default is two empty dictionaries, so nothing is
        sent for a character without an incubation.  Failures stay inside
        this standalone batch.
        """

        try:
            with s.flow_lock:
                claims = dict(s.gameplay_timed_offer_purchases)
            info = SeasonalActivityService._seasonal_hatch_info(self, claims)
            if not info.eggs:
                return
            SeasonalActivityService._seasonal_publish(
                self, s, None,
                results=((USER_HATCH_EGGS_INFO_COMMAND, info),),
                label="restore-hatch-eggs",
            )
        except Exception as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] could not restore hatch eggs: {exc}"
            )

    def _seasonal_mark_returner(
            self, s: Session, persisted_state, now: datetime) -> bool:
        campaign = load_cameback_campaign(
            self.static_database_path, CAMEBACK_2026,
        )
        if _hook_active_event(self, campaign.event_id, now) is None:
            return False
        with s.flow_lock:
            if RECALL_RETURNER_KEY in s.gameplay_timed_offer_purchases:
                return False
        previous = _parse_saved_at(getattr(persisted_state, "saved_at", ""))
        if previous is None:
            # A character without an earlier save is new, not returning.
            return False
        if (now - previous).total_seconds() <= RECALL_INACTIVITY_SECONDS:
            return False
        with s.flow_lock:
            claims = dict(s.gameplay_timed_offer_purchases)
            claims[RECALL_RETURNER_KEY] = date_int(now)
            s.gameplay_timed_offer_purchases = claims
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] recall 2026: character returned after "
            f"{(now - previous).days} days of inactivity"
        )
        return True

    def _seasonal_record_crafting(
            self, s: Session, recipe_id: str, amount: int) -> None:
        """Add crafted anniversary cakes to the server total."""

        try:
            definition = load_crafting_cakes_definition(
                self.static_database_path
            )
            if str(recipe_id) != definition.recipe_id or int(amount) <= 0:
                return
            if _hook_active_event(
                self, definition.event_id, _hook_now(self),
            ) is None:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] crafted {recipe_id!r} outside "
                    f"{definition.event_id!r}; server total unchanged"
                )
                return
            store = SeasonalActivityService._seasonal_store(self)
            if store is None:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] no seasonal state store; "
                    f"{amount} crafted cakes not counted"
                )
                return
            total = store.add_crafting_cakes(definition.recipe_id, int(amount))
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] anniversary cakes crafted: "
                f"+{int(amount)} (server total {total})"
            )
        except Exception as exc:
            # The craft itself is already committed and saved.
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] could not count crafted cakes: {exc}"
            )

    # -- egg incubation ----------------------------------------------------

    def _seasonal_hatch_info(self, claims: dict) -> UserHatchEggsInfo:
        eggs = []
        for egg in load_hatch_egg_definitions(
            self.static_database_path
        ).values():
            deadline = _claim(claims, HATCH_DEADLINE_KEY.format(egg=egg.egg_id))
            if deadline <= 0:
                continue
            eggs.append((
                egg.sql_index,
                _claim(claims, HATCH_FEEDS_KEY.format(egg=egg.egg_id)),
                datetime.fromtimestamp(deadline, timezone.utc),
            ))
        return hatch_eggs_info(eggs)

    def _seasonal_send_hatch_info(self, s: Session) -> None:
        with s.flow_lock:
            claims = dict(s.gameplay_timed_offer_purchases)
        SeasonalActivityService._seasonal_publish(
            self, s, None,
            results=((
                USER_HATCH_EGGS_INFO_COMMAND,
                SeasonalActivityService._seasonal_hatch_info(self, claims),
            ),),
            label="hatch-eggs-info",
        )

    def _seasonal_hatch(self, s: Session, request: OperateHatchEggsRequest) -> None:
        egg = load_hatch_egg_definitions(self.static_database_path).get(
            request.egg_id
        )
        if egg is None:
            raise SeasonalActivityError(f"unknown hatch egg {request.egg_id!r}")
        snapshot = SeasonalActivityService._seasonal_snapshot(self, s)
        now = self._seasonal_now()
        now_seconds = int(now.timestamp())
        deadline_key = HATCH_DEADLINE_KEY.format(egg=egg.egg_id)
        feeds_key = HATCH_FEEDS_KEY.format(egg=egg.egg_id)
        deadline = _claim(snapshot.claims, deadline_key)
        feeds = _claim(snapshot.claims, feeds_key)
        claims = dict(snapshot.claims)
        mutation = None
        changed = True
        required_level = load_purchase_item_definition(
            self.static_database_path, egg.egg_id,
        ).required_level
        if request.operation == HATCH_OPERATION_START:
            if deadline > 0:
                # The widget reopened on an egg whose time it lost; resend.
                changed = False
            else:
                if snapshot.level < int(required_level):
                    raise SeasonalActivityError(
                        f"{egg.egg_id!r} requires level {required_level}"
                    )
                # Using the Growth item consumes it (db.growth.item text).
                mutation = SeasonalActivityService._seasonal_mutation(
                    self, snapshot, costs=((egg.egg_id, 1),),
                )
                claims[deadline_key] = now_seconds + egg.total_seconds
                claims[feeds_key] = 0
        else:
            if deadline <= 0:
                raise SeasonalActivityError(f"{egg.egg_id!r} is not incubating")
            remaining = deadline - now_seconds - feeds * egg.feed_seconds
            if request.operation == HATCH_OPERATION_FEED:
                if snapshot.level < int(required_level):
                    raise SeasonalActivityError(
                        f"feeding {egg.egg_id!r} requires level {required_level}"
                    )
                if remaining <= 0:
                    raise SeasonalActivityError(f"{egg.egg_id!r} is ready")
                mutation = SeasonalActivityService._seasonal_mutation(
                    self, snapshot,
                    costs=((egg.cost_template, egg.cost_amount),),
                )
                claims[feeds_key] = feeds + 1
            elif request.operation == HATCH_OPERATION_HATCH:
                if remaining > 0:
                    raise SeasonalActivityError(
                        f"{egg.egg_id!r} needs {remaining} more seconds"
                    )
                rng = self._seasonal_random()
                # db text: a base chance (+ per feed) of the first reward,
                # "or" the second.  Local choice: with more than two entries
                # the remainder is uniform (both authored rows have two).
                chance = min(
                    HATCH_CHANCE_SCALE,
                    egg.base_chance + feeds * egg.feed_chance,
                )
                if len(egg.awards) == 1 or rng.randrange(HATCH_CHANCE_SCALE) < chance:
                    award = egg.awards[0]
                else:
                    award = egg.awards[1 + rng.randrange(len(egg.awards) - 1)]
                mutation = SeasonalActivityService._seasonal_mutation(
                    self, snapshot,
                    rewards=parse_loot_awards(award, snapshot.class_name),
                    rng=rng,
                )
                claims.pop(deadline_key, None)
                claims.pop(feeds_key, None)
        SeasonalActivityService._seasonal_publish(
            self, s, snapshot,
            mutation=mutation,
            claims=claims if changed else None,
            results=((
                USER_HATCH_EGGS_INFO_COMMAND,
                SeasonalActivityService._seasonal_hatch_info(self, claims),
            ),),
            label="hatch-eggs",
            reason=f"hatch egg operation {request.operation}",
        )

    # -- wishing well / selective chests -----------------------------------

    def _seasonal_wishing_well(
            self, s: Session, request: SelectWishingWellRequest) -> None:
        snapshot = SeasonalActivityService._seasonal_snapshot(self, s)
        item = next(
            (
                entry for entry in snapshot.inventory.items
                if int(entry.item_id) == int(request.item_id)
            ),
            None,
        )
        if item is None:
            raise SeasonalActivityError(
                f"inventory item {request.item_id} does not exist"
            )
        if int(item.item_id) in snapshot.inventory.equipment_slots:
            raise SeasonalActivityError("equipped items cannot be wished on")
        definition = load_wishing_well_definitions(
            self.static_database_path
        ).get(item.template_id)
        if definition is None:
            raise SeasonalActivityError(
                f"{item.template_id!r} has no _Template_WishingWell row"
            )
        template = load_purchase_item_definition(
            self.static_database_path, item.template_id,
        )
        if template.item_category not in {"WishingWell", "Selective"}:
            raise SeasonalActivityError(
                f"{item.template_id!r} is category {template.item_category!r}"
            )
        if snapshot.level < int(template.required_level):
            raise SeasonalActivityError(
                f"{item.template_id!r} requires level {template.required_level}"
            )
        # The lamp widget always sends 1; the selective slider ranges over
        # the stack (SelectiveRewardWidget::ShowWithItem 0x1405c261c).
        maximum = 1 if template.item_category == "WishingWell" else int(item.amount)
        if not 1 <= request.amount <= maximum:
            raise SeasonalActivityError(
                f"amount {request.amount} outside 1..{maximum}"
            )
        if len(request.selections) != definition.select_max:
            raise SeasonalActivityError(
                f"{definition.template_id!r} needs {definition.select_max} "
                f"selection(s), got {len(request.selections)}"
            )
        rewards: list[LootAward] = []
        for index in request.selections:
            if not 0 <= index < len(definition.options):
                raise SeasonalActivityError(f"wish option {index} does not exist")
            option = definition.options[index]
            if option.offer_group:
                group = resolve_offer_group(
                    self.static_database_path, option.resource_id,
                    class_name=snapshot.class_name, player_level=snapshot.level,
                )
                rewards.extend(
                    LootAward(award.template_id, award.amount * request.amount)
                    for award in group
                )
            else:
                rewards.append(LootAward(
                    option.resource_id, int(option.amount) * request.amount,
                ))
        mutation = SeasonalActivityService._seasonal_mutation(
            self, snapshot,
            consume_items=((int(item.item_id), request.amount),),
            rewards=tuple(rewards),
        )
        claims = dict(snapshot.claims)
        key = WISHING_WELL_KEY.format(template=item.template_id)
        claims[key] = _claim(claims, key) + request.amount
        SeasonalActivityService._seasonal_publish(
            self, s, snapshot, mutation=mutation, claims=claims,
            label="wishing-well", reason="wishing well used",
        )

    # -- anniversary cakes -------------------------------------------------

    def _seasonal_cakes_result(self, claims: dict) -> CraftingCakesInfoResult:
        definition = load_crafting_cakes_definition(self.static_database_path)
        store = SeasonalActivityService._seasonal_store(self)
        total = (
            store.crafting_cakes_total(definition.recipe_id)
            if store is not None else 0
        )
        return CraftingCakesInfoResult(
            min(int(total), 0x7FFFFFFF),
            tuple(
                milestone.milestone_id for milestone in definition.milestones
                if _claim(claims, CAKE_CLAIM_KEY.format(
                    recipe=definition.recipe_id,
                    milestone=milestone.milestone_id,
                ))
            ),
        )

    def _seasonal_cakes_info(self, s: Session) -> None:
        with s.flow_lock:
            claims = dict(s.gameplay_timed_offer_purchases)
        SeasonalActivityService._seasonal_publish(
            self, s, None,
            results=((
                GET_CRAFTING_CAKES_INFO_RESULT_COMMAND,
                SeasonalActivityService._seasonal_cakes_result(self, claims),
            ),),
            label="crafting-cakes-info",
        )

    def _seasonal_cakes_award(
            self, s: Session, request: CraftingCakesAwardRequest) -> None:
        definition = load_crafting_cakes_definition(self.static_database_path)
        milestone = definition.milestone(request.milestone_id)
        if milestone is None:
            raise SeasonalActivityError(
                f"unknown cake milestone {request.milestone_id!r}"
            )
        snapshot = SeasonalActivityService._seasonal_snapshot(self, s)
        key = CAKE_CLAIM_KEY.format(
            recipe=definition.recipe_id, milestone=milestone.milestone_id,
        )
        if _claim(snapshot.claims, key):
            raise SeasonalActivityError(
                f"cake milestone {milestone.milestone_id!r} already claimed"
            )
        store = SeasonalActivityService._seasonal_store(self)
        total = store.crafting_cakes_total(definition.recipe_id) if store else 0
        if total < milestone.times:
            raise SeasonalActivityError(
                f"server total {total} is below {milestone.times}"
            )
        # The cake reward NPC and the crafted cake belong to the item's event.
        if self._seasonal_active_event(
            definition.event_id, self._seasonal_now(),
        ) is None:
            raise SeasonalActivityError(f"{definition.event_id!r} is not active")
        rewards = resolve_offer_group(
            self.static_database_path, milestone.award_group,
            class_name=snapshot.class_name, player_level=snapshot.level,
        )
        mutation = SeasonalActivityService._seasonal_mutation(
            self, snapshot, rewards=rewards,
        )
        claims = dict(snapshot.claims)
        claims[key] = 1
        SeasonalActivityService._seasonal_publish(
            self, s, snapshot, mutation=mutation, claims=claims,
            results=((
                GET_CRAFTING_CAKES_INFO_RESULT_COMMAND,
                SeasonalActivityService._seasonal_cakes_result(self, claims),
            ),),
            label="crafting-cakes-award",
            reason=f"cake milestone {milestone.milestone_id} claimed",
        )

    # -- comeback / recall sign-in calendars -------------------------------

    def _seasonal_cameback_eligible(
            self, snapshot: _SeasonalSnapshot, campaign, now: datetime) -> bool:
        if not self._seasonal_event_allows(
            campaign.event_id, now, snapshot.level,
        ):
            return False
        if campaign.key == CAMEBACK_2026:
            # "Return Check-in" is the returning (inactive) player's calendar.
            return _claim(snapshot.claims, RECALL_RETURNER_KEY) > 0
        return True

    def _seasonal_cameback_result(
            self, snapshot: _SeasonalSnapshot, campaign,
            claims: dict, now: datetime) -> CameBackSigninInfoResult:
        today = date_int(now)
        if not SeasonalActivityService._seasonal_cameback_eligible(
            self, snapshot, campaign, now,
        ):
            # State 0 with no signed day: every slot shows locked.
            return CameBackSigninInfoResult(0, 0, today, ())
        days = _claim(claims, CAMEBACK_DAYS_KEY.format(event=campaign.event_id))
        last = _claim(claims, CAMEBACK_LAST_KEY.format(event=campaign.event_id))
        if days >= len(campaign.days):
            # All days claimed: report today so the client shows "All are
            # Claimed" instead of offering a fifteenth day.
            last = max(last, today)
        bonuses = tuple(
            day for day in campaign.bonus_days
            if _claim(claims, CAMEBACK_BONUS_KEY.format(
                event=campaign.event_id, day=day,
            ))
        )
        # Local choice: the double reward needs a same-day recharge, which
        # this server does not model, so double claims always equal days.
        return CameBackSigninInfoResult(days, days, last, bonuses)

    def _seasonal_cameback_info(self, s: Session, campaign_key: str) -> None:
        campaign = load_cameback_campaign(self.static_database_path, campaign_key)
        snapshot = SeasonalActivityService._seasonal_snapshot(self, s)
        SeasonalActivityService._seasonal_publish(
            self, s, None,
            results=((
                _CAMEBACK_RESULTS[campaign_key],
                SeasonalActivityService._seasonal_cameback_result(
                    self, snapshot, campaign, snapshot.claims,
                    self._seasonal_now(),
                ),
            ),),
            label=f"{campaign_key}-info",
        )

    def _seasonal_cameback_award(
            self, s: Session, campaign_key: str,
            request: CameBackSigninAwardRequest) -> None:
        campaign = load_cameback_campaign(self.static_database_path, campaign_key)
        snapshot = SeasonalActivityService._seasonal_snapshot(self, s)
        now = self._seasonal_now()
        if not SeasonalActivityService._seasonal_cameback_eligible(
            self, snapshot, campaign, now,
        ):
            raise SeasonalActivityError(
                f"{campaign.event_id!r} sign-in is not available"
            )
        days_key = CAMEBACK_DAYS_KEY.format(event=campaign.event_id)
        last_key = CAMEBACK_LAST_KEY.format(event=campaign.event_id)
        days = _claim(snapshot.claims, days_key)
        claims = dict(snapshot.claims)
        if request.bonus:
            day = campaign.day(request.value)
            if day is None or not day.bonus_award:
                raise SeasonalActivityError(
                    f"day {request.value} has no bonus reward"
                )
            bonus_key = CAMEBACK_BONUS_KEY.format(
                event=campaign.event_id, day=day.day,
            )
            if _claim(claims, bonus_key):
                raise SeasonalActivityError(f"day {day.day} bonus already claimed")
            if days < day.day:
                raise SeasonalActivityError(
                    f"day {day.day} bonus needs {day.day} signed days"
                )
            raw_award = day.bonus_award
            claims[bonus_key] = 1
        elif request.value == CAMEBACK_MODE_DAILY:
            today = date_int(now)
            if _claim(claims, last_key) >= today:
                raise SeasonalActivityError("today's reward was already claimed")
            if days >= len(campaign.days):
                raise SeasonalActivityError("every calendar day is claimed")
            raw_award = campaign.days[days].award
            claims[days_key] = days + 1
            claims[last_key] = today
        elif request.value == CAMEBACK_MODE_DOUBLE:
            raise SeasonalActivityError(
                "double rewards need a same-day recharge (not modeled)"
            )
        else:
            raise SeasonalActivityError(f"unsupported sign-in mode {request.value}")
        rewards = parse_loot_awards(raw_award, snapshot.class_name)
        if campaign_key == CAMEBACK_2026:
            store = SeasonalActivityService._seasonal_store(self)
            coins = sum(
                award.amount for award in rewards
                if award.template_id == RETURNED_COIN_TEMPLATE
            )
            if coins and store is not None and store.recall_link(
                snapshot.character_id
            ) is not None:
                # The linked code owner's 10% share is read from this total.
                claims[RECALL_LINKED_COINS_KEY] = (
                    _claim(claims, RECALL_LINKED_COINS_KEY) + coins
                )
        mutation = SeasonalActivityService._seasonal_mutation(
            self, snapshot, rewards=rewards,
        )
        SeasonalActivityService._seasonal_publish(
            self, s, snapshot, mutation=mutation, claims=claims,
            results=((
                _CAMEBACK_RESULTS[campaign_key],
                SeasonalActivityService._seasonal_cameback_result(
                    self, snapshot, campaign, claims, now,
                ),
            ),),
            label=f"{campaign_key}-award",
            reason=f"{campaign.event_id} sign-in claimed",
        )

    # -- 2026 recall link --------------------------------------------------

    def _seasonal_character_name(self, character_id: int) -> str:
        store = getattr(self, "account_store", None)
        reader = getattr(store, "get_profile", None)
        profile = reader(int(character_id)) if reader is not None else None
        return str(getattr(profile, "name", "") or "")

    def _seasonal_saved_claims(self, character_id: int) -> dict:
        store = getattr(self, "account_store", None)
        state = store.get(int(character_id)) if store is not None else None
        return dict(getattr(state, "timed_offer_purchases", {}) or {})

    def _seasonal_recall_result(
            self, snapshot: _SeasonalSnapshot, claims: dict) -> CameBackUserInfo2026:
        store = SeasonalActivityService._seasonal_store(self)
        if _claim(claims, RECALL_RETURNER_KEY):
            link = (
                store.recall_link(snapshot.character_id)
                if store is not None else None
            )
            return CameBackUserInfo2026(
                RECALL_USER_RETURNER,
                link.code if link is not None else "",
                SeasonalActivityService._seasonal_character_name(
                    self, link.summoner_id,
                ) if link is not None else "",
                link.linked_at if link is not None else "",
                1 if _claim(claims, RECALL_LINK_REWARD_KEY) else 0,
                0, (), (),
            )
        links = (
            store.recall_links_for(snapshot.character_id)
            if store is not None else ()
        )
        return CameBackUserInfo2026(
            RECALL_USER_SUMMONER,
            recall_code_for(snapshot.character_id),
            "",
            "",
            1 if _claim(claims, RECALL_INVITE_REWARD_KEY) else 0,
            _claim(claims, RECALL_COINS_CLAIMED_KEY),
            tuple(link.returner_name for link in links),
            tuple(
                _claim(
                    SeasonalActivityService._seasonal_saved_claims(
                        self, link.returner_id,
                    ),
                    RECALL_LINKED_COINS_KEY,
                )
                for link in links
            ),
        )

    def _seasonal_recall_info(self, s: Session) -> None:
        snapshot = SeasonalActivityService._seasonal_snapshot(self, s)
        SeasonalActivityService._seasonal_publish(
            self, s, None,
            results=((
                GET_CAMEBACK_USER_INFO_RESULT_2026_COMMAND,
                SeasonalActivityService._seasonal_recall_result(
                    self, snapshot, snapshot.claims,
                ),
            ),),
            label="recall-2026-info",
        )

    def _seasonal_recall_require_event(
            self, snapshot: _SeasonalSnapshot, now: datetime) -> None:
        campaign = load_cameback_campaign(self.static_database_path, CAMEBACK_2026)
        if not self._seasonal_event_allows(
            campaign.event_id, now, snapshot.level,
        ):
            raise SeasonalActivityError(f"{campaign.event_id!r} is not available")

    def _seasonal_recall_claim(
            self, s: Session, request: RecallBindOrPointsRequest) -> None:
        snapshot = SeasonalActivityService._seasonal_snapshot(self, s)
        now = self._seasonal_now()
        SeasonalActivityService._seasonal_recall_require_event(self, snapshot, now)
        store = SeasonalActivityService._seasonal_store(self)
        if store is None:
            raise SeasonalActivityError("no seasonal state store")
        groups = load_recall_award_groups(self.static_database_path)
        returner = _claim(snapshot.claims, RECALL_RETURNER_KEY) > 0
        claims = dict(snapshot.claims)
        if request.claim == RECALL_CLAIM_LINK_REWARD:
            if returner:
                if store.recall_link(snapshot.character_id) is None:
                    raise SeasonalActivityError("returner is not linked")
                key, group = RECALL_LINK_REWARD_KEY, groups.bind_group
            else:
                if not store.recall_links_for(snapshot.character_id):
                    raise SeasonalActivityError("no player used this code")
                key, group = RECALL_INVITE_REWARD_KEY, groups.invite_group
            if _claim(claims, key):
                raise SeasonalActivityError("link reward already claimed")
            rewards = resolve_offer_group(
                self.static_database_path, group,
                class_name=snapshot.class_name, player_level=snapshot.level,
            )
            claims[key] = 1
        elif request.claim == RECALL_CLAIM_COINS:
            if returner:
                raise SeasonalActivityError("only code owners claim coin shares")
            linked = sum(
                _claim(
                    SeasonalActivityService._seasonal_saved_claims(
                        self, link.returner_id,
                    ),
                    RECALL_LINKED_COINS_KEY,
                )
                for link in store.recall_links_for(snapshot.character_id)
            )
            earned = linked // RECALL_SUMMONER_SHARE_DIVISOR
            claimed = _claim(claims, RECALL_COINS_CLAIMED_KEY)
            if earned <= claimed:
                raise SeasonalActivityError("no Returned Coins to claim")
            rewards = (LootAward(RETURNED_COIN_TEMPLATE, earned - claimed),)
            claims[RECALL_COINS_CLAIMED_KEY] = earned
        else:
            raise SeasonalActivityError(f"unsupported recall claim {request.claim}")
        mutation = SeasonalActivityService._seasonal_mutation(
            self, snapshot, rewards=rewards,
        )
        SeasonalActivityService._seasonal_publish(
            self, s, snapshot, mutation=mutation, claims=claims,
            results=((
                GET_CAMEBACK_USER_INFO_RESULT_2026_COMMAND,
                SeasonalActivityService._seasonal_recall_result(
                    self, snapshot, claims,
                ),
            ),),
            label="recall-2026-claim",
            reason=f"recall claim {request.claim}",
        )

    def _seasonal_recall_bind(
            self, s: Session, request: RecallBindUserRequest) -> None:
        snapshot = SeasonalActivityService._seasonal_snapshot(self, s)
        now = self._seasonal_now()
        SeasonalActivityService._seasonal_recall_require_event(self, snapshot, now)
        if not _claim(snapshot.claims, RECALL_RETURNER_KEY):
            raise RecallLinkError(
                RECALL_CODE_INVALID_MESSAGE,
                "only returning (inactive) players enter a code",
            )
        store = SeasonalActivityService._seasonal_store(self)
        if store is None:
            raise SeasonalActivityError("no seasonal state store")
        if store.recall_link(snapshot.character_id) is not None:
            raise RecallLinkError(
                RECALL_LINK_CONFLICT_MESSAGE, "player is already linked",
            )
        summoner_id = parse_recall_code(request.code)
        if (
            summoner_id == snapshot.character_id
            or not SeasonalActivityService._seasonal_character_name(
                self, summoner_id,
            )
            or _claim(
                SeasonalActivityService._seasonal_saved_claims(self, summoner_id),
                RECALL_RETURNER_KEY,
            )
        ):
            # Codes belong to active (non-returning) players other than self.
            raise RecallLinkError(
                RECALL_CODE_NOT_FOUND_MESSAGE,
                f"recall code {request.code!r} names no eligible player",
            )
        try:
            store.link_recall(
                snapshot.character_id,
                summoner_id,
                returner_name=str(getattr(snapshot.character, "name", "") or ""),
                code=recall_code_for(summoner_id),
                linked_at=now.strftime("%Y-%m-%dT%H:%M:%S"),
                limit=RECALL_LINK_LIMIT,
            )
        except RecallLinkLimit as exc:
            raise RecallLinkError(RECALL_LINK_LIMIT_MESSAGE, str(exc)) from exc
        except RecallLinkConflict as exc:
            raise RecallLinkError(RECALL_LINK_CONFLICT_MESSAGE, str(exc)) from exc
        except SeasonalStateError as exc:
            raise RecallLinkError(RECALL_CODE_INVALID_MESSAGE, str(exc)) from exc
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] recall 2026: character "
            f"{snapshot.character_id} linked to code owner {summoner_id}"
        )
        SeasonalActivityService._seasonal_recall_info(self, s)
