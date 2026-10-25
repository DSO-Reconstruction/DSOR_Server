"""Trade between players (a DSOR addition; protocol/trade_protocol.py).

One player asks another (same map, both on a DSOR client that sent
TradeHello); the other accepts; both fill an offer (whole or partial stacks
from the backpack or the extra bag, and gold); each accepts the offers as
they stand, and any change takes both acceptances back. When both have
accepted, the exchange is made at once for both, or not at all:

* both accepted offers are frozen while electing one executor; the two
  session mutation gates exclude competing inventory commands and retirement;
* both inventories are snapshotted, both mutations computed outside flow locks: the
  offered stacks are consumed from the giver (InventoryMutationBuilder.consume)
  and restored in the receiver under a fresh id from the receiver's own id
  space (item ids are per character, see rekey_inventory_items);
* each side is committed under its own flow_lock only if its inventory,
  currency and next id are still the snapshot (two session_flow locks may never
  be held together, runtime/concurrency.py); if the second side changed in
  between, the first is put back;
* both sides get the delta, their CurrencyChanged and the final allocation
  (the vendor sale's batch), both are saved and the store flushed.

The registry is shared by the gameplay servers (login, characters) of the
process, under one plain lock never held while taking a session's flow_lock
or mutation gate. Busy trades reject offer, cancel and duplicate ready input.
"""

from __future__ import annotations

import copy
from contextlib import ExitStack
from functools import wraps
import threading
import time
import weakref
from dataclasses import dataclass, field, replace

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.runtime.outbound import OutboundCapacityError
from dso_server.protocol.commerce_protocol import load_purchase_item_definition
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder
from dso_server.protocol.trade_protocol import (
    PHASE_CLOSED, PHASE_DONE, PHASE_INVITED, PHASE_OPEN, PHASE_REQUESTED,
    TRADE_ANSWER_COMMAND, TRADE_CANCEL_COMMAND, TRADE_HELLO_COMMAND, TRADE_MAX_ITEMS,
    TRADE_OFFER_COMMAND, TRADE_READY_COMMAND, TRADE_REQUEST_COMMAND,
    TradeItem, TradeState, command_id_of, decode_trade_answer, decode_trade_cancel,
    decode_trade_hello, decode_trade_offer, decode_trade_ready, decode_trade_request,
    encode_trade_state,
)

_LOCK = threading.Lock()
# An invitation nobody answers lapses (and stops blocking both players).
TRADE_INVITE_SECONDS = 60.0
_SERVERS: "weakref.WeakSet" = weakref.WeakSet()
# Sessions whose client shows trades, by id(session) (a game client must never
# receive TradeState).
_CAPABLE: dict[int, Session] = {}


@dataclass
class _Side:
    character_id: int
    name: str
    gold: int = 0
    items: list[tuple[int, int]] = field(default_factory=list)
    ready: bool = False


@dataclass(frozen=True)
class _AcceptedSide:
    character_id: int
    name: str
    gold: int
    items: tuple[tuple[int, int], ...]
    ready: bool


def _snapshot_side(side: _Side) -> _AcceptedSide:
    return _AcceptedSide(side.character_id, side.name, side.gold, tuple(side.items), side.ready)


def _serialized_notification(method):
    @wraps(method)
    def invoke(trade, *args, **kwargs):
        with trade.notification_lock:
            return method(trade, *args, **kwargs)
    return invoke


@dataclass
class _Trade:
    requester: _Side
    invited: _Side
    open: bool = False
    created: float = 0.0
    # Set while one thread makes the exchange (both acceptances may arrive on
    # two gameplay servers' threads at once).
    busy: bool = False
    notification_lock: object = field(default_factory=threading.RLock, repr=False)

    def side(self, character_id: int) -> _Side:
        return self.requester if self.requester.character_id == character_id else self.invited

    def other(self, character_id: int) -> _Side:
        return self.invited if self.requester.character_id == character_id else self.requester


# Both participants' character ids map to the same trade.
_TRADES: dict[int, _Trade] = {}


class TradeService:

    # -- lookups ---------------------------------------------------------------

    @staticmethod
    def _trade_sessions():
        with _LOCK:
            servers = tuple(_SERVERS)
        for server in servers:
            for session in server._active_gameplay_sessions():
                if session.gameplay_character is not None:
                    yield server, session

    @staticmethod
    def _trade_session(character_id: int):
        return next(((server, s) for server, s in TradeService._trade_sessions()
                     if int(s.gameplay_character.character_id) == int(character_id)), (None, None))

    @staticmethod
    def _trade_capable(s: Session | None) -> bool:
        with _LOCK:
            return s is not None and _CAPABLE.get(id(s)) is s

    @staticmethod
    def _trade_send(s: Session | None, state: TradeState) -> None:
        if not TradeService._trade_capable(s):
            return
        packet = encode_trade_state(state)
        try:
            s.send_reliable_ordered(packet, track=True, label="trade-state")
        except (OSError, OutboundCapacityError, RuntimeError) as exc:
            TradeService._trade_delivery_failed(s, exc)

    @staticmethod
    def _trade_delivery_failed(s: Session, exc: Exception) -> None:
        # Maintenance retires this endpoint. The other participant must still
        # receive its result, and a delivery failure cannot undo a saved trade.
        s.transport_failure_reason = s.transport_failure_reason or str(exc)
        print(f"[{GAMEPLAY_SERVICE_NAME}] trade delivery failed for {s.addr}: {exc}")

    @staticmethod
    def _trade_items_of(s: Session, side: _Side) -> tuple[TradeItem, ...]:
        # Inventories are copy-on-write: this reference is a stable snapshot.
        inventory = s.gameplay_inventory
        by_id = {int(i.item_id): i for i in (inventory.items if inventory else ())}
        return tuple(
            TradeItem(item_id, by_id[item_id].template_id, amount, int(by_id[item_id].field_08))
            for item_id, amount in side.items if item_id in by_id
        )

    @staticmethod
    @_serialized_notification
    def _trade_publish(trade: _Trade, messages: dict[int, str] | None = None) -> None:
        """Send each participant the trade as the server holds it."""
        messages = messages or {}
        with _LOCK:
            if (_TRADES.get(trade.requester.character_id) is not trade
                    or _TRADES.get(trade.invited.character_id) is not trade):
                return
            sides = (_snapshot_side(trade.requester), _snapshot_side(trade.invited))
            opened = trade.open
        for index, side in enumerate(sides):
            other = sides[1 - index]
            _, s = TradeService._trade_session(side.character_id)
            _, partner = TradeService._trade_session(other.character_id)
            if opened:
                phase = PHASE_OPEN
            else:
                phase = PHASE_REQUESTED if index == 0 else PHASE_INVITED
            TradeService._trade_send(s, TradeState(
                phase, other.name, side.gold, tuple(side.items), side.ready, other.gold,
                TradeService._trade_items_of(partner, other) if partner is not None else (),
                other.ready, messages.get(side.character_id, "")))

    @staticmethod
    @_serialized_notification
    def _trade_close(
        trade: _Trade, messages: dict[int, str], phase: int = PHASE_CLOSED,
        *, force: bool = False,
    ) -> bool:
        with _LOCK:
            if trade.busy and not force:
                return False
            for side in (trade.requester, trade.invited):
                if _TRADES.get(side.character_id) is trade:
                    del _TRADES[side.character_id]
        for side in (trade.requester, trade.invited):
            _, s = TradeService._trade_session(side.character_id)
            TradeService._trade_send(s, TradeState(phase, trade.other(side.character_id).name,
                                                   message=messages.get(side.character_id, "")))
        return True

    @staticmethod
    def _trade_retire_session(session: Session) -> None:
        """Release abandoned invitations/trades when a participant disconnects."""
        with _LOCK:
            _CAPABLE.pop(id(session), None)
            character = session.gameplay_character
            trade = _TRADES.get(int(character.character_id)) if character is not None else None
        if trade is not None:
            TradeService._trade_close(trade, {
                trade.other(int(character.character_id)).character_id:
                    f"{character.name} is no longer here",
            })

    # -- commands --------------------------------------------------------------

    def _handle_trade_command(self, s: Session, payload: bytes) -> None:
        with _LOCK:
            _SERVERS.add(self)
        command = command_id_of(payload)
        CharacterService._accept_gameplay_state_command(s, command)
        with s.flow_lock:
            character = s.gameplay_character
        if character is None:
            return
        me = int(character.character_id)
        if command == TRADE_HELLO_COMMAND:
            decode_trade_hello(payload)
            with _LOCK:
                for key, session in list(_CAPABLE.items()):
                    if not session.connected:
                        del _CAPABLE[key]
                _CAPABLE[id(s)] = s
                s.gameplay_retire_callback = TradeService._trade_retire_session
            return
        if command == TRADE_REQUEST_COMMAND:
            self._trade_request(s, me, character.name, decode_trade_request(payload))
            return
        with _LOCK:
            trade = _TRADES.get(me)
        if trade is None:
            TradeService._trade_send(s, TradeState(PHASE_CLOSED))
            return
        other = trade.other(me)
        _, partner = TradeService._trade_session(other.character_id)
        if partner is None:
            TradeService._trade_close(trade, {me: f"{other.name} is no longer here"})
            return
        if command == TRADE_CANCEL_COMMAND:
            decode_trade_cancel(payload)
            TradeService._trade_close(trade, {other.character_id: f"{character.name} cancelled the trade"})
        elif command == TRADE_ANSWER_COMMAND:
            accept = decode_trade_answer(payload)
            with _LOCK:
                if (_TRADES.get(me) is not trade or trade.busy
                        or trade.open or trade.invited.character_id != me):
                    return
                if accept:
                    trade.open = True
            if not accept:
                TradeService._trade_close(trade, {other.character_id: f"{character.name} declined the trade"})
                return
            TradeService._trade_publish(trade)
        elif command == TRADE_OFFER_COMMAND:
            if trade.open:
                self._trade_offer(s, trade, me, decode_trade_offer(payload))
        elif command == TRADE_READY_COMMAND:
            ready = decode_trade_ready(payload)
            with _LOCK:
                if _TRADES.get(me) is not trade or trade.busy or not trade.open:
                    return
                trade.side(me).ready = ready
                execute = trade.requester.ready and trade.invited.ready
                trade.busy = execute
                accepted = (_snapshot_side(trade.requester), _snapshot_side(trade.invited))
            if execute:
                try:
                    self._trade_execute(trade, accepted)
                except Exception:
                    # A failed encoder/persistence callback must not leave a
                    # reusable accepted trade or block future invitations.
                    TradeService._trade_close(trade, {}, force=True)
                    raise
                finally:
                    with _LOCK:
                        trade.busy = False
            else:
                TradeService._trade_publish(trade)

    def _trade_request(self, s: Session, me: int, my_name: str, target_name: str) -> None:
        wanted = str(target_name).strip().casefold()
        target = next((t for _, t in TradeService._trade_sessions()
                       if t.gameplay_character.name.casefold() == wanted), None)
        message = None
        if target is None:
            message = f"{target_name} could not be found"
        elif target is s:
            message = "you cannot trade with yourself"
        elif not TradeService._trade_capable(target):
            message = f"{target.gameplay_character.name} cannot trade (their client has no trade)"
        elif target.gameplay_map != s.gameplay_map:
            message = f"{target.gameplay_character.name} is not on your map"
        if message is not None:
            TradeService._trade_send(s, TradeState(PHASE_CLOSED, str(target_name), message=message))
            return
        them = int(target.gameplay_character.character_id)
        now = time.monotonic()
        replaced = None
        with _LOCK:
            # Invitations nobody answered in time lapse.
            for trade in {id(t): t for t in _TRADES.values()}.values():
                if not trade.open and now - trade.created > TRADE_INVITE_SECONDS:
                    for side in (trade.requester, trade.invited):
                        _TRADES.pop(side.character_id, None)
            mine, theirs = _TRADES.get(me), _TRADES.get(them)
            if mine is not None and mine is theirs and not mine.open and mine.invited.character_id == me:
                # They asked us first: asking them back accepts.
                mine.open = True
                trade = mine
            elif mine is not None and (mine.open or mine.invited.character_id == me):
                message = "you are already trading"
            elif theirs is not None and theirs is not mine:
                message = f"{target.gameplay_character.name} is already trading"
            else:
                # Our own unanswered request to someone else is replaced.
                if mine is not None:
                    replaced = mine
                    for side in (mine.requester, mine.invited):
                        _TRADES.pop(side.character_id, None)
                trade = _Trade(_Side(me, my_name), _Side(them, target.gameplay_character.name), created=now)
                _TRADES[me] = _TRADES[them] = trade
        if replaced is not None and replaced is not trade:
            TradeService._trade_close(replaced, {replaced.invited.character_id: f"{my_name} withdrew the trade request"})
        if message is not None:
            TradeService._trade_send(s, TradeState(PHASE_CLOSED, str(target_name), message=message))
            return
        TradeService._trade_publish(trade)

    def _trade_offer(self, s: Session, trade: _Trade, me: int, offer) -> None:
        with s.flow_lock:
            inventory = s.gameplay_inventory
            currency = s.gameplay_currency_state
        side = trade.side(me)
        by_id = {int(i.item_id): i for i in inventory.items}
        items: list[tuple[int, int]] = []
        for item_id, amount in offer.items[:TRADE_MAX_ITEMS]:
            item = by_id.get(int(item_id))
            if (item is None or any(i == item_id for i, _ in items)
                    or int(item_id) in inventory.equipment_slots
                    or not (int(item_id) in inventory.storage_58 or int(item_id) in inventory.storage_80)
                    or getattr(item, "quest_id", "")):
                continue
            items.append((int(item_id), max(1, min(int(amount), int(item.amount)))))
        with _LOCK:
            if _TRADES.get(me) is not trade or trade.busy or not trade.open:
                return
            side.gold = max(0, min(int(offer.gold), int(currency.gold) if currency is not None else 0))
            side.items = items
            trade.requester.ready = trade.invited.ready = False
        TradeService._trade_publish(trade)

    # -- the exchange ----------------------------------------------------------

    def _trade_execute(self, trade: _Trade, accepted: tuple[_AcceptedSide, _AcceptedSide]) -> None:
        (_, sa), (_, sb) = (TradeService._trade_session(trade.requester.character_id),
                            TradeService._trade_session(trade.invited.character_id))
        if sa is None or sb is None:
            TradeService._trade_close(trade, {}, force=True)
            return
        # The dispatcher's per-session gate excludes the counterpart's item,
        # travel and commerce commands while both inventories are exchanged.
        # Do not hold _LOCK here: the counterpart must be able to observe busy
        # and return from READY/OFFER/CANCEL while we await its session gate.
        with ExitStack() as locks:
            for session in sorted((sa, sb), key=id):
                lock = session.gameplay_mutation_lock
                if not lock.acquire(timeout=15.0):
                    raise TimeoutError("timed out waiting for a trade participant")
                locks.callback(lock.release)
            if any(not s.connected or s.retired or s.gameplay_exit_transition_started for s in (sa, sb)):
                TradeService._trade_close(trade, {}, force=True)
                return
            self._trade_execute_locked(trade, ((accepted[0], sa), (accepted[1], sb)))

    def _trade_execute_locked(self, trade: _Trade, sides) -> None:
        accepted_by_id = {side.character_id: side for side, _ in sides}
        def other_side(side):
            return accepted_by_id[trade.other(side.character_id).character_id]
        snapshots = {}
        for side, s in sides:
            # This may emit membership/bag updates. Complete it before either
            # inventory changes; the later "player trade" saves skip delivery.
            CharacterService._refresh_premium_bags(s)
            CharacterService._sync_inventory_live_health(s)
            with s.flow_lock:
                snapshots[side.character_id] = (s.gameplay_inventory, s.gameplay_next_item_id, s.gameplay_currency_state)
        try:
            builders = {side.character_id: InventoryMutationBuilder(snapshots[side.character_id][0], snapshots[side.character_id][1])
                        for side, _ in sides}
            given: dict[int, list] = {}
            for side, _ in sides:
                builder = builders[side.character_id]
                out = []
                for item_id, amount in side.items:
                    item = builder.item(item_id)
                    if int(item_id) in builder.inventory.equipment_slots or not (
                            int(item_id) in builder.inventory.storage_58 or int(item_id) in builder.inventory.storage_80):
                        raise ValueError(f"{side.name}'s offered item moved")
                    if int(amount) > int(item.amount):
                        raise ValueError(f"{side.name}'s offered stack shrank")
                    whole = int(item.amount)
                    part = copy.deepcopy(builder.consume(item_id, int(amount)))
                    if int(amount) != whole:
                        part.field_30 = int(part.field_30) * int(amount) // max(1, whole)
                        part.field_34 = int(part.field_34) * int(amount) // max(1, whole)
                    part.amount = int(amount)
                    out.append(part)
                given[side.character_id] = out
            for side, _ in sides:
                receiver = other_side(side)
                builder = builders[receiver.character_id]
                for item in given[side.character_id]:
                    moved = copy.deepcopy(item)
                    moved.item_id = builder.next_item_id
                    moved.field_18 = moved.item_id & 0xFFFF
                    definition = load_purchase_item_definition(self.static_database_path, moved.template_id)
                    try:
                        builder.restore_item(moved, ingredient_bag=definition.ingredient_bag)
                    except ValueError:
                        raise ValueError(f"{receiver.name}'s bags are full")
            results = {}
            for side, _ in sides:
                other = other_side(side)
                currency = snapshots[side.character_id][2]
                gold = int(currency.gold) - side.gold + other.gold
                if int(currency.gold) < side.gold:
                    raise ValueError(f"{side.name} no longer has that much gold")
                results[side.character_id] = (builders[side.character_id].finish(), replace(currency, amount=gold))
        except ValueError as exc:
            with _LOCK:
                trade.requester.ready = trade.invited.ready = False
            message = str(exc)
            TradeService._trade_publish(trade, {trade.requester.character_id: message, trade.invited.character_id: message})
            return

        committed = []
        for side, s in sides:
            inventory, next_id, currency = snapshots[side.character_id]
            mutation, new_currency = results[side.character_id]
            with s.flow_lock:
                unchanged = (s.gameplay_inventory is inventory and s.gameplay_next_item_id == next_id
                             and s.gameplay_currency_state == currency)
                if unchanged:
                    s.gameplay_inventory = mutation.inventory
                    s.gameplay_next_item_id = mutation.next_item_id
                    s.gameplay_currency_state = new_currency
            if not unchanged:
                # Put back the side already committed, if it is still ours.
                for done_side, done_s in committed:
                    old_inventory, old_next, old_currency = snapshots[done_side.character_id]
                    done_mutation, done_currency = results[done_side.character_id]
                    with done_s.flow_lock:
                        if done_s.gameplay_inventory is done_mutation.inventory:
                            done_s.gameplay_inventory = old_inventory
                            done_s.gameplay_next_item_id = old_next
                            done_s.gameplay_currency_state = old_currency
                with _LOCK:
                    trade.requester.ready = trade.invited.ready = False
                message = "an inventory changed, check the offers again"
                TradeService._trade_publish(trade, {trade.requester.character_id: message, trade.invited.character_id: message})
                return
            committed.append((side, s))

        # Publish both committed inventories durably before any encoding or
        # socket work can fail. Attempt both saves even if one persistence
        # callback raises; retain its error and suppress success notifications.
        persistence_errors = []
        for _side, s in sides:
            try:
                CharacterService._request_account_save(s, "player trade", flush=True)
            except Exception as exc:
                persistence_errors.append(exc)
        store = getattr(self, "account_store", None)
        if store is not None:
            try:
                store.flush()
            except Exception as exc:
                persistence_errors.append(exc)
        if persistence_errors:
            raise persistence_errors[0]

        for side, s in sides:
            mutation, currency = results[side.character_id]
            packet, bits = encode_server_command_batch((
                ServerCommand(INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID),
                ServerCommand(CURRENCY_CHANGED_COMMAND, replace(currency, notify=True), LOCAL_PLAYER_ACTOR_ID),
                ServerCommand(INVENTORY_INFO_COMMAND, mutation.final, LOCAL_PLAYER_ACTOR_ID),
            ), include_terminator=True)
            try:
                CharacterService._send_world_loot_state_batch(s, packet, bits, label="player-trade")
            except (OSError, OutboundCapacityError, RuntimeError) as exc:
                TradeService._trade_delivery_failed(s, exc)
        print(f"[{GAMEPLAY_SERVICE_NAME}] trade {trade.requester.name} -> {trade.invited.name}: "
              f"{trade.requester.items} + {trade.requester.gold} gold for "
              f"{trade.invited.items} + {trade.invited.gold} gold")
        TradeService._trade_close(trade, {}, PHASE_DONE, force=True)
