"""Atomic hand-in rewards for quests which grant authored status effects.

Current client quest rewards name native effects, without duration/value
overrides. Their Persistent, real-time, Replace/Deny and ResetRunTime fields
come from _Template_StatusEffect, independently of XML permanent=False.
"""

from types import SimpleNamespace
import time

from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.commerce_protocol import load_purchase_item_definition
from dso_server.protocol.consumable_protocol import load_consumable_catalog
from dso_server.protocol.gameplay_commands import LOCAL_PLAYER_ACTOR_ID
from dso_server.protocol.gameplay_protocol import STATUS_EFFECT_COMMAND, MESSAGE_COMMAND, StatusEffect
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder
from dso_server.protocol.item_receipt_protocol import (
    ITEM_RECEIVED_TEMPLATE_CATEGORIES, ItemReceiptGrant, build_item_receipts, item_received_message,
)
from dso_server.protocol.loot_protocol import _credit_currency
from dso_server.protocol.player_init_protocol import (
    CURRENCY_CHANGED_COMMAND, INVENTORY_INFO_COMMAND, ServerCommand, encode_server_command_batch,
)
from dso_server.protocol.skill_book_protocol import experience_commands
from dso_server.services.base import CharacterService
from dso_server.services.player_progression import load_player_progression_catalog


def prepare_quest_status_rewards(server, session, quest):
    """Resolve content outside flow_lock; None leaves ordinary quests alone."""
    class_name = CLASS_NAMES[int(session.gameplay_character.class_id)]
    rewards = quest.rewards_for_class(class_name)
    if not any(reward.kind == "statuseffect" for reward in rewards):
        return None
    catalog = load_consumable_catalog(server.static_database_path)
    names = load_status_runtime_catalog(server.static_database_path).effect_ids
    effects, items, gold, andermant, xp = [], [], 0, 0, 0
    for reward in rewards:
        if reward.kind == "statuseffect":
            effect_id = names.get(reward.value)
            definition = catalog.definition_for_effect(effect_id) if effect_id is not None else None
            if definition is None or definition.effect_resource_id != reward.value:
                raise ValueError(f"unknown quest status reward {reward.value!r}")
            # These seven authored effects have only passive start modifiers.
            # Reject unsupported content before hand-in instead of inventing
            # immediate health/child-program semantics for future rows.
            if definition.child_effects or definition.immediate_health or definition.periodic_health:
                raise ValueError(f"quest status reward requires an unsupported effect program: {reward.value}")
            effects.append(definition)
        elif reward.kind in {"vc", "rc", "xp", "dynamicxp"}:
            amount = int(float(reward.value or 0))
            if reward.kind == "vc":
                gold += amount
            elif reward.kind == "rc":
                andermant += amount
            elif reward.kind == "xp":
                xp += amount
            else:
                xp += server._quest_dynamic_xp(session, float(reward.value or 0))
        elif reward.kind == "loot" and reward.value:
            amount = max(1, int(reward.amount))
            if reward.value in {"money_vc", "money_vc_gold"}:
                gold += amount
            elif reward.value == "money_rc":
                andermant += amount
            else:
                items.append((load_purchase_item_definition(server.static_database_path, reward.value), amount))
    return SimpleNamespace(
        catalog=catalog, effects=tuple(effects), items=tuple(items), gold=gold,
        andermant=andermant, xp=xp, path=server.static_database_path,
        progression=load_player_progression_catalog(server.static_database_path) if xp > 0 else None,
    )


def stage_quest_status_rewards_locked(session, plan, cleanup):
    """Prepare inventory, XP, currency and effects without changing live state."""
    inventory = cleanup.inventory if cleanup is not None else session.gameplay_inventory
    next_item_id = cleanup.next_item_id if cleanup is not None else session.gameplay_next_item_id
    if inventory is None or session.gameplay_currency_state is None:
        raise ValueError("quest rewards need initialized inventory and currency")
    commands = []
    if cleanup is not None:
        commands.extend(ServerCommand(INVENTORY_INFO_COMMAND, value, LOCAL_PLAYER_ACTOR_ID)
                        for value in (cleanup.delta, cleanup.final))
    character = session.gameplay_character
    points = tuple(session.gameplay_talent_category_points)
    transition = None
    if plan.xp > 0:
        transition = plan.progression.advance_player(
            class_id=int(character.class_id), current_level=max(1, int(character.level)),
            current_xp=max(int(character.progression_u32), int(points[0]) if len(points) == 4 else 0),
            amount=plan.xp,
        )
        if transition is not None:
            commands.extend(experience_commands(plan.path, character.class_id, transition,
                                               actor_id=LOCAL_PLAYER_ACTOR_ID,
                                               quick_slots=session.gameplay_quick_slots))
    currency = session.gameplay_currency_state
    if plan.gold > 0:
        currency = _credit_currency(currency, "money_vc_gold", plan.gold)
    if plan.andermant > 0:
        currency = _credit_currency(currency, "money_rc", plan.andermant)
    if currency != session.gameplay_currency_state:
        commands.append(ServerCommand(CURRENCY_CHANGED_COMMAND, currency, LOCAL_PLAYER_ACTOR_ID))
    if plan.items:
        builder = InventoryMutationBuilder(inventory, next_item_id)
        for definition, amount in plan.items:
            builder.add_item(definition, amount, item_level=(transition.level if transition else character.level))
        mutation = builder.finish()
        grants = tuple(ItemReceiptGrant(definition.template_id, amount, materialized=True,
                       include_template_id=definition.item_category in ITEM_RECEIVED_TEMPLATE_CATEGORIES)
                       for definition, amount in plan.items)
        receipts, next_item_id = build_item_receipts(
            inventory, mutation.inventory, (item.item_id for item in mutation.changed_items),
            mutation.replacements, grants, next_item_id=mutation.next_item_id,
        )
        inventory = mutation.inventory
        commands.append(ServerCommand(INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID))
        commands.extend(ServerCommand(MESSAGE_COMMAND, item_received_message(receipt), LOCAL_PLAYER_ACTOR_ID)
                        for receipt in receipts)
        commands.append(ServerCommand(INVENTORY_INFO_COMMAND, mutation.final, LOCAL_PLAYER_ACTOR_ID))
    # The existing installer implements native exclusivity and clock-reset
    # rules under the caller's lock. Give it detached containers for preflight.
    fields = ("gameplay_persistent_status_effects", "gameplay_timed_status_effect_expiries",
              "gameplay_runtime_status_effects", "gameplay_projectile_timers")
    staged = SimpleNamespace(**{name: dict(getattr(session, name)) for name in fields},
                             gameplay_next_status_effect_instance_id=session.gameplay_next_status_effect_instance_id)
    applied, retired_timers, removals = [], [], []
    server_tick = int(session.gameplay_chase_server_tick) & 0xFFFFFFFF
    for definition in plan.effects:
        installed = CharacterService._install_instant_status_entry(
            staged, definition, plan.catalog, server_tick=server_tick,
            now=time.perf_counter(), now_epoch=time.time(),
        )
        if installed is None:  # Authored Deny keeps the existing effect/expiry.
            continue
        entry, removed, timers, instance_id = installed
        applied.append((entry, definition, instance_id))
        removals.extend(removed)
        retired_timers.extend(timers)
    status = tuple(StatusEffect(False, (entry,)) for entry, _definition, _id in applied)
    if removals:
        status += (StatusEffect(False, tuple(removals)),)
    commands.extend(ServerCommand(STATUS_EFFECT_COMMAND, value, LOCAL_PLAYER_ACTOR_ID) for value in status)
    packet = encode_server_command_batch(tuple(commands), include_terminator=True) if commands else None
    return SimpleNamespace(inventory=inventory, next_item_id=next_item_id, currency=currency,
                           transition=transition, points=points, staged=staged, fields=fields,
                           applied=applied, retired_timers=retired_timers, status=status, packet=packet)


def commit_quest_status_rewards_locked(session, result):
    session.gameplay_inventory = result.inventory
    session.gameplay_next_item_id = result.next_item_id
    session.gameplay_currency_state = result.currency
    if result.transition is not None:
        session.gameplay_character.progression_u32 = result.transition.xp
        session.gameplay_character.level = result.transition.level
        if len(result.points) == 4:
            session.gameplay_talent_category_points = (result.transition.xp, *result.points[1:])
    for name in result.fields:
        setattr(session, name, getattr(result.staged, name))
    session.gameplay_next_status_effect_instance_id = result.staged.gameplay_next_status_effect_instance_id


def finish_quest_status_rewards(server, session, result):
    """Persist rewards, finish authoritative work, return the first send error."""
    CharacterService._request_account_save(session, "quest status rewards", flush=True)
    for timer in result.retired_timers:
        timer.cancel()
    for _entry, definition, instance_id in result.applied:
        CharacterService._schedule_consumable_timers(session, instance_id, definition,
                                                    database_path=server.static_database_path)
    operations = []
    if result.packet is not None:
        operations.append(lambda: CharacterService._send_world_loot_state_batch(
            session, *result.packet, label="quest-status-rewards"))
    if result.status and session.gameplay_presence_callback is not None:
        operations.append(lambda: session.gameplay_presence_callback(session, ("status_effects", result.status)))
    operations.append(lambda: CharacterService._refresh_player_combat_vitals(session))
    if result.transition is not None and result.transition.level_up:
        operations.append(lambda: CharacterService._achievement_on_level_up(
            session, int(session.gameplay_character.class_id), result.transition.level))
    failure = None
    for operation in operations:
        try:
            operation()
        except OSError as exc:
            failure = failure or exc
    # A failed outbound queue must not skip level-driven vitality changes.
    CharacterService._request_account_save(session, "quest status rewards", flush=True)
    return failure
