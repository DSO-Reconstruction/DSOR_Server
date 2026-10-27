"""Per-character authoritative player cast deadlines.

Only incoming player casts reserve these timers. Server projectile impacts,
multi-hit continuations and summon casts deliberately never pass this gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
import time

from dso_server.gameplay.status_modifier_runtime import (
    load_status_modifier_catalog, resolve_modifier_value,
    snapshot_active_status_entries,
)

TICKS_PER_SECOND = 25.0
# How early (seconds) a cast may arrive against its cadence or cooldown
# deadline. One packet delayed in transit pushes the next deadline back by
# that delay, so the following on-time cast reaches the server early: with a
# single tick (40 ms) every network hiccup dropped a cast the client had
# already drawn (Hunting Arrow flew, no damage). Deadlines advance from the
# previous one, so the grace never adds up to extra casts.
CAST_GRACE_SECONDS = max(
    1.0 / TICKS_PER_SECOND,
    float(os.getenv("DSO_SKILL_CAST_GRACE_SECONDS", "0.15")),
)


@dataclass
class PlayerSkillTimingState:
    cooldowns: dict[str, float] = field(default_factory=dict)
    cast_ready_at: float = 0.0
    cast_category: str = ""


def _finite(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def snapshot_player_skill_timing(state, *, now=None, now_epoch=None) -> dict:
    now = time.perf_counter() if now is None else float(now)
    epoch = time.time() if now_epoch is None else float(now_epoch)
    if not isinstance(state, PlayerSkillTimingState):
        return {}
    return {
        "cooldowns": {
            str(category).casefold(): epoch + deadline - now
            for category, value in state.cooldowns.items()
            if (deadline := _finite(value)) > now
        },
        "cast_ready_at": (
            epoch + state.cast_ready_at - now
            if _finite(state.cast_ready_at) > now else 0.0
        ),
        "cast_category": state.cast_category if state.cast_ready_at > now else "",
    }


def restore_player_skill_timing(document, *, now=None, now_epoch=None):
    now = time.perf_counter() if now is None else float(now)
    epoch = time.time() if now_epoch is None else float(now_epoch)
    state = PlayerSkillTimingState()
    if not isinstance(document, dict):
        return state
    cooldowns = document.get("cooldowns", {})
    if isinstance(cooldowns, dict):
        for category, expiry in cooldowns.items():
            remaining = _finite(expiry) - epoch
            if isinstance(category, str) and category and remaining > 0:
                state.cooldowns[category.casefold()] = now + min(remaining, 86400.0)
    remaining = _finite(document.get("cast_ready_at")) - epoch
    if remaining > 0:
        state.cast_ready_at = now + min(remaining, 60.0)
        state.cast_category = str(document.get("cast_category", "")).casefold()
    return state


def reset_player_skill_timing(session):
    with session.flow_lock:
        session.gameplay_player_skill_timing = PlayerSkillTimingState()


def player_skill_timing_state(session):
    """Get mutable state; caller must own the session flow lock."""
    state = getattr(session, "gameplay_player_skill_timing", None)
    if not isinstance(state, PlayerSkillTimingState):
        state = session.gameplay_player_skill_timing = PlayerSkillTimingState()
    return state


@dataclass(frozen=True)
class SkillTimingReservation:
    state: PlayerSkillTimingState
    category: str
    previous_cooldown: float
    cooldown: float
    previous_cast_ready_at: float
    cast_ready_at: float
    previous_cast_category: str


def reserve_player_skill_timing(session, geometry, speed=1.0, *, now=None):
    """Reserve an incoming cast, returning None when it is premature.

    ``CAST_GRACE_SECONDS`` absorbs packet jitter for both cadence and
    cooldowns. Advancing from the previous deadline prevents that tolerance
    accumulating into extra casts. Callers pass the command's arrival time as
    ``now`` so server-side queueing is never mistaken for an early cast.
    """
    now = time.perf_counter() if now is None else float(now)
    category = str(geometry.cooldown_category or geometry.resource_id).casefold()
    with session.flow_lock:
        state = player_skill_timing_state(session)
        previous_cooldown = state.cooldowns.get(category, 0.0)
        previous_ready = state.cast_ready_at
        previous_category = state.cast_category
        force = geometry.force_execution and category != previous_category
        if previous_cooldown > now + CAST_GRACE_SECONDS + 1e-6 or (
            not force
            and previous_ready > now + CAST_GRACE_SECONDS + 1e-6
        ):
            return None
        scale = max(0.01, _finite(speed, 1.0)) if geometry.scale_skill_timing else 1.0
        frames = max(1, round(
            geometry.skill_unblock_frame * geometry.skill_timing_multiplier / scale
        ))
        # Authored escape/charge skills can interrupt recovery. Their own
        # cooldown still gates the interrupt, and their recovery starts now.
        ready = (now if force else max(now, previous_ready)) + frames / TICKS_PER_SECOND
        cooldown = max(now, previous_cooldown) + max(
            0.0, _finite(geometry.cooldown_seconds)
        )
        state.cast_ready_at = ready
        state.cast_category = category
        state.cooldowns = {
            key: deadline for key, deadline in state.cooldowns.items() if deadline > now
        }
        if cooldown > now:
            state.cooldowns[category] = cooldown
        return SkillTimingReservation(
            state, category, previous_cooldown, cooldown, previous_ready, ready, previous_category,
        )


def rollback_player_skill_timing(session, reservation):
    """Undo an unpaid cast without overwriting a concurrent reset/effect."""
    with session.flow_lock:
        state = getattr(session, "gameplay_player_skill_timing", None)
        if state is not reservation.state:
            return
        if state.cast_ready_at == reservation.cast_ready_at:
            state.cast_ready_at = reservation.previous_cast_ready_at
            state.cast_category = reservation.previous_cast_category
        if state.cooldowns.get(reservation.category) == reservation.cooldown:
            if reservation.previous_cooldown:
                state.cooldowns[reservation.category] = reservation.previous_cooldown
            else:
                state.cooldowns.pop(reservation.category, None)


def timing_actor_variables(initial_values, instructions):
    """Resolve talent actor-variable programs before cooldown operands."""
    variables = dict(initial_values)
    for kind, arguments, stack_count, values in instructions:
        if str(kind).casefold() != "actorvariable" or len(arguments) < 3:
            continue
        amount = resolve_modifier_value(arguments[0], values, variables)
        if amount is None or not math.isfinite(amount):
            continue
        amount *= stack_count
        key = arguments[2].split("#", 1)[0].strip().casefold()
        current = variables.get(key, 0.0)
        variables[key] = (
            current * (1.0 + amount)
            if arguments[1].casefold() == "relative" else current + amount
        )
    return variables


def apply_skill_cooldown_effect(session, definition, database_path, *, now=None, tick=False):
    """Apply an accepted cooldown effect, acquiring the session flow lock."""
    catalog = load_status_modifier_catalog(database_path)
    with session.flow_lock:
        return apply_skill_cooldown_effect_locked(
            session, definition, database_path, now=now, tick=tick, catalog=catalog,
        )


def apply_skill_cooldown_effect_locked(session, definition, database_path, *, now=None, tick=False, catalog=None):
    """Apply an accepted start/tick's ActiveCoolDown/ActorCoolDownReset.

    The caller owns flow_lock. Passing a preloaded catalog avoids I/O while
    that lock is held.
    This does not roll chance or enforce status exclusivity: callers do that
    before applying the effect. A cooldown reset never cancels cast recovery.
    """
    now = time.perf_counter() if now is None else float(now)
    catalog = catalog if catalog is not None else load_status_modifier_catalog(database_path)
    program = catalog.definitions.get(int(definition.effect_id))
    if program is None:
        return False
    modifiers = program.tick_modifiers if tick else program.start_modifiers
    if not any(m.kind in {"activecooldown", "actorcooldownreset"} for m in modifiers):
        return False
    sources = snapshot_active_status_entries(
        getattr(session, "gameplay_persistent_status_effects", {}),
        getattr(session, "gameplay_timed_status_effect_expiries", {}),
        getattr(session, "gameplay_runtime_status_effects", {}),
        getattr(session, "gameplay_item_set_effects", {}),
        getattr(session, "gameplay_socket_effects", {}),
        now_monotonic=now,
    )
    instructions = []
    for source in sources:
        source_program = catalog.definitions.get(int(source.effect_id))
        if source_program is None:
            continue
        stacks = min(source_program.max_stack_size, max(1, int(getattr(source, "field_18", 0)) + 1))
        instructions.extend(
            (modifier.kind, modifier.arguments, stacks, tuple(getattr(source, "values", ())))
            for modifier in source_program.start_modifiers
        )
    variables = timing_actor_variables(catalog.actor_variables, instructions)
    state = player_skill_timing_state(session)
    changed = False
    for modifier in modifiers:
        arguments = modifier.arguments
        if modifier.kind == "actorcooldownreset":
            for category in arguments:
                key = category.split("#", 1)[0].strip().casefold()
                changed |= state.cooldowns.pop(key, None) is not None
        elif modifier.kind == "activecooldown" and len(arguments) >= 3:
            amount = resolve_modifier_value(arguments[0], getattr(definition, "values", ()), variables)
            if amount is None or not math.isfinite(amount):
                continue
            for category in arguments[2:]:
                key = category.split("#", 1)[0].strip().casefold()
                remaining = max(0.0, state.cooldowns.get(key, 0.0) - now)
                if not remaining:
                    continue
                updated = max(0.0, remaining * max(0.0, 1.0 + amount)
                              if arguments[1].casefold() == "relative" else remaining + amount)
                if updated != remaining:
                    changed = True
                    if updated:
                        state.cooldowns[key] = now + updated
                    else:
                        state.cooldowns.pop(key, None)
    return changed
