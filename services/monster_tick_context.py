"""Lock-separated inputs and clock commits for the monster map worker."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


class MonsterTickContextService:
    """Prepare immutable tick inputs without overlapping state domains."""

    @staticmethod
    def _snapshot_monster_tick_owner(
        s: Session,
        generation: int,
        combat,
        *,
        now: float,
    ):
        """Read the owner clock without retaining ``flow_lock``."""

        with s.flow_lock:
            if (
                not s.connected
                or generation != s.gameplay_chase_generation
                or s.gameplay_combat_state is not combat
            ):
                return None
            previous_at = s.gameplay_chase_last_at
            if previous_at is None:
                previous_at = now
            elapsed = max(0.0, min(0.5, now - previous_at))
            previous_server_tick = int(s.gameplay_chase_server_tick)
            anchor = getattr(s, "gameplay_chase_clock_anchor", None)
            if (
                anchor is None
                or anchor[0] != generation
                or anchor[1] is not combat
            ):
                anchor = (generation, combat, previous_at, previous_server_tick)
            # Quantize total wall time once. Rounding each interval (and
            # forcing at least one tick) made delayed/early tick pairs run
            # faster than the client clock, accumulating seconds of lead.
            ticks = int(
                max(0.0, now - anchor[2]) * SERVER_TICKS_PER_SECOND + 1e-7
            )
            server_tick = (anchor[3] + ticks) & 0xFFFFFFFF
            clock_advance = (previous_server_tick - server_tick) & 0xFFFFFFFF
            if 0 < clock_advance < 0x80000000:
                # A fresh client Move advanced the clock since the anchor.
                server_tick = previous_server_tick
                anchor = (generation, combat, now, server_tick)
            return SimpleNamespace(
                last_move=s.gameplay_last_move,
                character=s.gameplay_character,
                remote_actor_id=s.gameplay_remote_actor_id,
                elapsed=elapsed,
                previous_server_tick=previous_server_tick,
                clock_anchor=anchor,
                server_tick=server_tick,
            )

    @staticmethod
    def _commit_monster_tick_owner(
        s: Session,
        generation: int,
        combat,
        snapshot,
        *,
        now: float,
    ) -> bool:
        """Revalidate and publish the owner clock after map mutation."""

        with s.flow_lock:
            if (
                not s.connected
                or generation != s.gameplay_chase_generation
                or s.gameplay_combat_state is not combat
            ):
                return False
            s.gameplay_chase_last_at = now
            # Movement can advance this clock during the combat transaction.
            # Never overwrite that newer client-derived value.
            if (
                int(s.gameplay_chase_server_tick)
                == int(snapshot.previous_server_tick)
            ):
                s.gameplay_chase_server_tick = int(snapshot.server_tick)
                s.gameplay_chase_clock_anchor = snapshot.clock_anchor
            return True

    @staticmethod
    def _prepare_victim_feature_gates(candidate_target_sessions):
        """Resolve per-player combat gates before any state lock is taken.

        The owner is also a normal combat target in a single-player map and
        ``flow_lock`` is non-reentrant, so reading these from inside the
        critical section made the monster worker acquire its own lock twice.

        The two gates are deliberately distinct. Damage immunity nullifies
        incoming damage; it must not remove its owner from the aggro set.
        A defensive proc like ``ce2023_jewel_offensive_effect`` (8% on hit,
        three seconds, ``ActorFeature:off,Damage``) otherwise left every
        monster on the map with no target at all -- zero movements and zero
        attacks for the effect's duration, for every viewer, whatever spell
        was being cast.

        Both gates are answered from one disabled-feature set per session. The
        previous form asked three separate ``_player_victim_feature_locked``
        questions per player, each of which independently re-derived that set
        and took the player's ``flow_lock`` to read live victim debuffs. On a
        populated map the simulation thread paid that for every viewer, 25
        times a second, which is exactly the contention that delays hits.
        """

        damage_locked = set()
        untargetable = set()
        for session_id, target_session in candidate_target_sessions.items():
            disabled = CharacterService._player_disabled_actor_features(
                target_session
            )
            if not disabled:
                continue
            if "damage" in disabled:
                damage_locked.add(session_id)
            # Mirror ``_player_is_untargetable``: an explicit Targetable lock,
            # or the movement+skills pair that death, revival and hiding use.
            if "targetable" in disabled or (
                "movement" in disabled and "skills" in disabled
            ):
                untargetable.add(session_id)
        return frozenset(damage_locked), frozenset(untargetable)

    @staticmethod
    def _snapshot_monster_tick_viewers(viewers, combat):
        """Read every viewer's tick inputs with one ``flow_lock`` each.

        Visibility and periodic socket auras were previously two separate
        passes over the same sessions, so the map's single simulation thread
        acquired each viewer's lock twice per tick before it had done any
        simulation at all. Both reads are plain session state and are
        consistent when taken together, so they belong in one critical
        section.

        Returns ``(spawned_by_viewer, visible_actor_ids, has_periodic_auras)``.
        Viewers that disconnected, departed presence, or changed map between
        the caller's snapshot and this read are skipped. That liveness test
        now also gates the aura contribution, which it did not before: a
        departed socket's periodic auras could previously keep a map out of
        its idle state with no viewer left to receive the ticks.
        """

        spawned_by_viewer = {}
        visible_actor_ids: set[int] = set()
        has_periodic_auras = False
        for viewer_id, viewer in viewers.items():
            with viewer.flow_lock:
                if (
                    not viewer.connected
                    or viewer.gameplay_presence_departed
                    or viewer.gameplay_combat_state is not combat
                ):
                    continue
                spawned = set(viewer.gameplay_spawned_monster_actor_ids)
                periodic_auras = getattr(
                    getattr(
                        viewer,
                        "gameplay_socket_effect_evaluation",
                        None,
                    ),
                    "periodic_auras",
                    (),
                )
            spawned_by_viewer[viewer_id] = spawned
            visible_actor_ids.update(spawned)
            has_periodic_auras = has_periodic_auras or bool(periodic_auras)
        return spawned_by_viewer, visible_actor_ids, has_periodic_auras

    @staticmethod
    def _describe_idle_window(combat, tick_inputs) -> str:
        """Name the inputs that left a populated map simulating nothing.

        Deliberately reported per heartbeat window rather than per tick.
        Movement publication is throttled to one frame per actor every
        ``MONSTER_MOVEMENT_PUBLISH_INTERVAL_TICKS``, so an individual tick that
        publishes nothing is ordinary; a whole second of them, with actors
        active and none locked, is the reported freeze.
        """

        described = ", ".join(
            f"{name}={value}"
            for name, value in sorted((tick_inputs or {}).items())
        ) or "no tick inputs recorded"
        return (
            f"[{GAMEPLAY_SERVICE_NAME}] map simulated nothing for a whole "
            f"window: {len(combat.active_monster_actor_ids)} active actor(s), "
            f"{len(combat.disabled_monster_actor_ids())} locked; {described}"
        )

    @staticmethod
    def _report_monster_control(
            combat, movements, attacks=(), tick_inputs=None) -> None:
        """Log what the map's simulation is producing, and why it may not be.

        A client reporting frozen monsters gives no way to tell an actor the
        server has deliberately locked out (stun, freeze, root) from one the
        server is simulating normally whose packets never landed, nor either
        of those from a simulation that simply stopped producing. All three
        have different fixes and none is visible in an ordinary log.
        """

        movements = tuple(movements)
        attacks = tuple(attacks)
        if tick_inputs is not None:
            combat._last_tick_inputs = tick_inputs
        combat.simulation_ticks += 1
        combat.published_movements += len(movements)
        combat.published_attacks += len(attacks)
        now = time.perf_counter()
        if not combat._heartbeat_started_at:
            combat._heartbeat_started_at = now
        elapsed = now - combat._heartbeat_started_at
        if elapsed >= MONSTER_SIMULATION_HEARTBEAT_SECONDS:
            active = len(combat.active_monster_actor_ids)
            # Silence while there is genuinely nothing to simulate; a run of
            # zeroes is only meaningful against a populated map.
            if active or combat.published_movements:
                rate = combat.simulation_ticks / elapsed
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] simulation heartbeat: "
                    f"{rate:.1f} ticks/s, "
                    f"{combat.published_movements / elapsed:.1f} movements/s, "
                    f"{combat.published_attacks / elapsed:.1f} attacks/s, "
                    f"{active} active actor(s), "
                    f"{len(combat.disabled_monster_actor_ids())} locked"
                )
                if active and rate < 0.8 * SERVER_TICKS_PER_SECOND:
                    from dso_server.runtime import hotspot_sampler

                    hotspot_sampler.trigger(f"monster tick at {rate:.1f}/s")
            if (
                active
                and not combat.published_movements
                and not combat.published_attacks
            ):
                print(
                    CharacterService._describe_idle_window(
                        combat, combat._last_tick_inputs
                    )
                )
            combat.simulation_ticks = 0
            combat.published_movements = 0
            combat.published_attacks = 0
            combat._heartbeat_started_at = now

        for actor_id, stepped, walkable, elapsed_ticks in (
            combat.drain_teleport_reports()
        ):
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] actor {actor_id} published an "
                f"unwalkable step: {stepped:.2f} units in {elapsed_ticks} "
                f"tick(s), walkable {walkable:.2f}; the client must draw this "
                "as a jump"
            )

        transitions = combat.drain_monster_control_transitions()
        for actor_id, reason in transitions:
            if reason is None:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] actor {actor_id} may act "
                    "again"
                )
            else:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] actor {actor_id} locked out "
                    f"of Movement/RegularSkills by {reason}"
                )
        if not transitions:
            return
        disabled = combat.disabled_monster_actor_ids()
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] control summary: "
            f"{len(disabled)} locked of "
            f"{len(combat.active_monster_actor_ids)} active; "
            f"movements published this tick={len(tuple(movements))}"
        )

    @staticmethod
    def _prime_monster_tick_catalogs(combat) -> None:
        """Resolve lazy SQLite-backed monster data outside map locks."""

        if getattr(combat, "_monster_tick_catalogs_primed", False):
            return
        combat._mind_control_taunt_radius()
        definitions = {
            definition.resource_id: definition
            for monster in combat.monsters.values()
            for definition in (monster.attacks or (monster.attack,))
        }
        for definition in definitions.values():
            combat._load_monster_user_effects(definition)
            combat._load_monster_attack_effects(definition)
        combat._monster_tick_catalogs_primed = True

    @staticmethod
    def _prepare_monster_target_inputs(
        combat,
        combat_targets,
        *,
        due_attack_actor_ids,
        now: float,
    ):
        """Reserve aura cadence and calculate target stats outside map locks."""

        prepared_targets = []
        due_auras_by_actor = {}
        target_stats_by_actor = {}
        for actor_id, position, target_session in combat_targets:
            with target_session.flow_lock:
                if (
                    not target_session.connected
                    or target_session.gameplay_presence_departed
                    or target_session.gameplay_combat_state is not combat
                ):
                    continue
                evaluation = getattr(
                    target_session,
                    "gameplay_socket_effect_evaluation",
                    None,
                )
                due_periodic_auras = []
                for aura in tuple(
                    getattr(evaluation, "periodic_auras", ())
                ):
                    cadence_key = ("periodic-aura", aura.key)
                    cadence = target_session.gameplay_socket_event_state
                    if cadence_key not in cadence:
                        cadence[cadence_key] = now + max(
                            0.04, float(aura.interval_seconds)
                        )
                        continue
                    if now + 1e-9 < float(cadence[cadence_key]):
                        continue
                    cadence[cadence_key] = now + max(
                        0.04, float(aura.interval_seconds)
                    )
                    due_periodic_auras.append(aura)
                character = target_session.gameplay_character
                inventory = target_session.gameplay_inventory

            actor_id = int(actor_id)
            prepared_targets.append((actor_id, position, target_session))
            due_auras_by_actor[actor_id] = tuple(due_periodic_auras)
            if (
                actor_id not in due_attack_actor_ids
                and not due_periodic_auras
            ):
                continue
            target_stats_by_actor[actor_id] = calculate_player_combat_stats(
                combat.encounter.static_database_path,
                cache_owner=target_session,
                class_name=CLASS_NAMES.get(
                    int(getattr(character, "class_id", 0)), "mage"
                ),
                level=max(1, int(getattr(character, "level", 1))),
                inventory=inventory,
                modifier_sources=(
                    CharacterService._active_skill_modifier_sources(
                        target_session
                    )
                ),
            )

        return (
            tuple(prepared_targets),
            due_auras_by_actor,
            target_stats_by_actor,
        )
