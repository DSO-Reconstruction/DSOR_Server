"""Register authored player impact deadlines without delaying them at startup."""
from __future__ import annotations

from dso_server.runtime.transport import Session
from dso_server.services.base import CharacterService


class PlayerCastTimingService:
    """Share equal-deadline hits and preserve their original due time."""

    @staticmethod
    def _register_runtime_projectile_deadlines(
            s: Session, pending_hits, *, now: float, generation: int):
        """Register one scheduler callback per authored impact deadline.

        Area and multi-projectile skills can produce many target impacts at
        the exact same database-authored arrival time. Scheduling one handle
        per target made all of those redundant callbacks contend for the
        combat lock, even though impact resolution already emits the equal-
        deadline hits as one transaction. Keep every logical timer key for
        cancellation compatibility, but point them at one shared handle.

        Callers hold the session flow lock while replacing key mappings.
        """

        deadline_groups = {}
        for value in pending_hits:
            timer_key = (
                int(value.source_actor_id),
                int(value.cast_id),
                int(value.impact_index),
            )
            group_key = (
                int(value.source_actor_id),
                int(value.cast_id),
                float(value.due_at),
            )
            deadline_groups.setdefault(group_key, []).append(timer_key)

        timers = []
        old_timers = []
        old_timer_ids = set()
        for (_source_actor_id, _cast_id, due_at), timer_keys in (
                deadline_groups.items()):
            ordered_keys = tuple(
                sorted(timer_keys, key=lambda timer_key: timer_key[2])
            )
            for timer_key in ordered_keys:
                old_timer = s.gameplay_projectile_timers.pop(
                    timer_key, None
                )
                if (
                    old_timer is not None
                    and id(old_timer) not in old_timer_ids
                ):
                    old_timer_ids.add(id(old_timer))
                    old_timers.append(old_timer)
            timer = s.gameplay_deadline_timer(
                max(0.0, float(due_at) - float(now)),
                CharacterService._resolve_player_projectile_group,
                args=(
                    s,
                    int(generation),
                    ordered_keys,
                    float(due_at),
                ),
            )
            timer.absolute_deadline = float(due_at)
            timer.daemon = True
            for timer_key in ordered_keys:
                s.gameplay_projectile_timers[timer_key] = timer
            timers.append(timer)
        return tuple(timers), tuple(old_timers)

