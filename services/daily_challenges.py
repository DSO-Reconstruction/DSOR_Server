"""Daily challenge initialization and persistent selection/refresh commands."""

from dataclasses import replace
from datetime import datetime, timezone
import sqlite3
import time

from dso_server.gameplay.daily_challenges import (
    ACCEPTED, AVAILABLE, COMPLETED, ChallengeEligibility, DailyChallengeState,
    load_daily_challenge_catalog, load_tier_spender_challenges,
    load_tier_spender_shuffle_prices,
)
from dso_server.gameplay.quest_content import QUEST_STATE_CLOSED
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.daily_challenge_protocol import (
    DAILY_CHALLENGE_SELECTION_COMMAND, decode_daily_challenge_selection,
)
from dso_server.protocol.membership_protocol import parse_membership_expiry
from dso_server.protocol.membership_protocol import (
    PREMIUM_MEMBERSHIP, DELUXE_MEMBERSHIP,
)
from dso_server.protocol.reward_protocol import (
    SHUFFLE_TIER_SPENDER_DEALS_COMMAND,
    decode_tier_spender_shuffle,
)
from dso_server.protocol.player_init_protocol import (
    CURRENCY_CHANGED_COMMAND, TIMED_CHALLENGES_INFO_COMMAND, DAILY_CHALLENGE_PROGRESS_COMMAND,
    UPDATE_PROGRESS_TIER_SPENDER_CHALLENGES_COMMAND,
    ServerCommand, TimedChallengesInfo, UpdateProgressTierSpenderChallenges,
    encode_server_command_batch,
)
from dso_server.runtime.context import (
    LOCAL_PLAYER_ACTOR_ID, RELIABLE_BACKLOG_SOFT_LIMIT,
)
from dso_server.services.base import CharacterService


TIER_SPENDER_SHUFFLE_MIN_INTERVAL_SECONDS = 0.5


class DailyChallengeService:
    @staticmethod
    def _daily_challenge_now():
        return datetime.now(timezone.utc)

    @staticmethod
    def _restore_daily_challenge_state(s, persisted_state):
        s.gameplay_daily_challenges = DailyChallengeState.from_document(
            getattr(persisted_state, "daily_challenges", None))
        s.gameplay_tier_spender_challenges = (
            s.gameplay_daily_challenges.spender
            if s.gameplay_daily_challenges is not None else ()
        )

    @staticmethod
    def _daily_challenge_document(s):
        state = getattr(s, "gameplay_daily_challenges", None)
        return state.to_document() if state is not None else {}

    def _daily_challenge_context(self, s):
        catalog = load_daily_challenge_catalog(self.static_database_path)
        now = self._daily_challenge_now()
        active = self._event_catalog().active_event_flags(now)
        with s.flow_lock:
            character = s.gameplay_character
            wisdom_xp = s.gameplay_talent_category_points[1]
            facts = ChallengeEligibility(
                int(character.level), CLASS_NAMES[int(character.class_id)].casefold(),
                closed_quests=frozenset(q.casefold() for q, p in
                                       getattr(s, "gameplay_quest_log", {}).items()
                                       if p.state == QUEST_STATE_CLOSED),
                active_events=active,
            )
        # Talent progression queries static content outside the session lock.
        facts = replace(facts, wisdom_level=self.talent_database.progression_level(1, wisdom_xp))
        return catalog, now, facts

    def _tier_spender_offers(self, s, now, facts):
        today = now.date().isoformat()
        with s.flow_lock:
            current = s.gameplay_daily_challenges
            if current is not None and current.day == today and len(current.spender) == 12:
                return current.spender
            membership_active = s.gameplay_membership_active
            membership_type = s.gameplay_membership_type
        membership = "NoPrivilege"
        if membership_active:
            if membership_type == DELUXE_MEMBERSHIP:
                membership = "Deluxe"
            elif membership_type == PREMIUM_MEMBERSHIP:
                membership = "Premium"
        return load_tier_spender_challenges(
            self.static_database_path, facts, membership=membership
        )

    def _daily_challenge_initialization_info(self, s):
        catalog, now, facts = self._daily_challenge_context(s)
        spender = self._tier_spender_offers(s, now, facts)
        with s.flow_lock:
            state = catalog.board(s.gameplay_daily_challenges, now.date(), facts)
            if state is not None and len(state.spender) != 12:
                state = replace(state, spender=spender)
            s.gameplay_daily_challenges = state
            s.gameplay_tier_spender_challenges = (
                state.spender if state is not None else spender
            )
        # The retail client builds the spender UI from its cached timed
        # challenge maps when the shop tab opens. Include all 12 RC rewards
        # in the initialization batch so they are present before that refresh.
        return (
            state.info(include_spender=True)
            if state else TimedChallengesInfo(0, (), (), (), ())
        )

    @staticmethod
    def _daily_challenge_free_refresh(s, catalog, now):
        if (not s.gameplay_membership_active
                or s.gameplay_membership_type not in catalog.free_refresh_memberships):
            return False
        expiry = s.gameplay_membership_expires_at
        return bool(expiry) and parse_membership_expiry(expiry) > now

    def _handle_daily_challenge_selection(self, s, payload):
        try:
            request = decode_daily_challenge_selection(payload, s.current_message_bit_length)
        except ValueError as exc:
            print(f"[DrasaOnlineMapServer] rejected Daily Challenge selection: {exc}")
            return
        with s.flow_lock:
            if (s.gameplay_character is None or not s.connected or getattr(s, "retired", False)
                    or s.gameplay_exit_transition_started):
                return
        catalog, now, facts = self._daily_challenge_context(s)
        spender = self._tier_spender_offers(s, now, facts)
        commands = []
        with s.flow_lock:
            if (not s.connected or getattr(s, "retired", False)
                    or s.gameplay_exit_transition_started):
                return
            before = s.gameplay_daily_challenges
            state = catalog.board(before, now.date(), facts)
            if state is not None and len(state.spender) != 12:
                state = replace(state, spender=spender)
            # A request arriving after midnight must not act on a newly rolled
            # board, even when it happens to contain yesterday's template ID.
            current_day = before is not None and before.day == now.date().isoformat()
            entries = list(state.daily) if state else []
            for index, entry in enumerate(entries):
                if not current_day or entry.challenge_id != request.challenge_id:
                    continue
                definition = catalog.definitions[entry.challenge_id]
                if entry.state != AVAILABLE or not definition.eligible(facts):
                    break
                if not request.refresh:
                    entries[index] = replace(entry, state=ACCEPTED)
                else:
                    cost = (0 if self._daily_challenge_free_refresh(s, catalog, now)
                            else catalog.refresh_cost)
                    currency = s.gameplay_currency_state
                    if cost and (currency is None or currency.currency_type < cost):
                        break
                    replacement = catalog.choose(
                        definition.category, definition.tier, facts,
                        excluding=entry.challenge_id,
                    )
                    if replacement is None:
                        break
                    entries[index] = replacement.entry()
                    if cost:
                        currency = replace(currency, currency_type=currency.currency_type - cost,
                                           notify=True)
                        s.gameplay_currency_state = currency
                        commands.append(ServerCommand(
                            CURRENCY_CHANGED_COMMAND, currency, LOCAL_PLAYER_ACTOR_ID))
                state = replace(state, daily=tuple(entries))
                break
            s.gameplay_daily_challenges = state
            s.gameplay_client_command_state[DAILY_CHALLENGE_SELECTION_COMMAND] = request
            commands.append(ServerCommand(
                TIMED_CHALLENGES_INFO_COMMAND,
                state.info() if state else TimedChallengesInfo(0, (), (), (), ()),
                LOCAL_PLAYER_ACTOR_ID,
            ))
        CharacterService._accept_gameplay_state_command(s, DAILY_CHALLENGE_SELECTION_COMMAND)
        if state != before:
            CharacterService._request_account_save(s, "Daily Challenge selection")
        s.send_reliable_ordered(
            encode_server_command_batch(tuple(commands), include_terminator=True),
            track=True, label="daily-challenges",
        )
        self._daily_challenge_feed(s, 'RetryRewards', count=0)

    def _handle_tier_spender_shuffle(self, s, payload):
        """Charge the authored price and reroll only the requested spender tier."""

        try:
            tier_index = decode_tier_spender_shuffle(
                payload, getattr(s, "current_message_bit_length", None)
            )
        except ValueError as exc:
            print(f"[DrasaOnlineMapServer] rejected tier spender shuffle: {exc}")
            return

        # Do not stack another state-changing reroll behind a response the
        # client has not acknowledged yet. This keeps a lost frame from
        # blocking a whole run of later board and wallet updates.
        pending_shuffle = s.pending_reliable_count_for_label(
            "tier-spender-shuffle"
        )
        if pending_shuffle:
            now = time.perf_counter()
            with s.flow_lock:
                last_log = float(getattr(
                    s, "gameplay_last_tier_spender_shuffle_backpressure_log_at", 0.0
                ))
                if now - last_log >= 2.0:
                    s.gameplay_last_tier_spender_shuffle_backpressure_log_at = now
                    print(
                        "[DrasaOnlineMapServer] ignored tier spender shuffle "
                        "while previous shuffle response is unacknowledged; "
                        "no RC charged"
                    )
            return

        # A shuffle response is reliable-ordered. Flooding that stream with
        # repeated clicks can leave the client waiting behind an unacked
        # packet, so stop accepting new paid rerolls while its send queue is
        # already congested. Rejected clicks are never charged.
        backlog = s.reliable_backlog_size()
        backlog_limit = max(4, RELIABLE_BACKLOG_SOFT_LIMIT // 2)
        if backlog >= backlog_limit:
            now = time.perf_counter()
            with s.flow_lock:
                last_log = float(getattr(
                    s, "gameplay_last_tier_spender_shuffle_backpressure_log_at", 0.0
                ))
                if now - last_log >= 2.0:
                    s.gameplay_last_tier_spender_shuffle_backpressure_log_at = now
                    print(
                        "[DrasaOnlineMapServer] ignored tier spender shuffle "
                        f"while reliable backlog is {backlog}; no RC charged"
                    )
            return

        # Request indices and displayed offer groups use the same tier order:
        # 19/copper, 49/silver, 99/gold.
        tier_by_index = {
            0: ("copper", "copper", 0),
            1: ("silver", "silver", 4),
            2: ("gold", "gold", 8),
        }
        price_slot, offer_slot, offset = tier_by_index[tier_index]
        with s.flow_lock:
            if (s.gameplay_character is None or not s.connected
                    or getattr(s, "retired", False)
                    or s.gameplay_exit_transition_started):
                return
            now = time.perf_counter()
            if now - float(getattr(
                    s, "gameplay_last_tier_spender_shuffle_at", 0.0
            )) < TIER_SPENDER_SHUFFLE_MIN_INTERVAL_SECONDS:
                return
            s.gameplay_last_tier_spender_shuffle_at = now
            before = s.gameplay_daily_challenges
            if (before is None or before.day != self._daily_challenge_now().date().isoformat()
                    or len(before.spender) != 12):
                print("[DrasaOnlineMapServer] ignored tier spender shuffle without today's board")
                return
            existing_ids = frozenset(
                entry.challenge_id for entry in before.spender[offset:offset + 4]
            )
            membership_active = s.gameplay_membership_active
            membership_type = s.gameplay_membership_type

        try:
            prices = load_tier_spender_shuffle_prices(self.static_database_path)
            cost = prices[price_slot]
            membership = "NoPrivilege"
            if membership_active:
                if membership_type == DELUXE_MEMBERSHIP:
                    membership = "Deluxe"
                elif membership_type == PREMIUM_MEMBERSHIP:
                    membership = "Premium"
            _catalog, now, facts = self._daily_challenge_context(s)
            if before.day != now.date().isoformat():
                return
            replacements = load_tier_spender_challenges(
                self.static_database_path, facts, membership=membership,
                slots=(offer_slot,), excluded_ids=existing_ids,
            )
            if len(replacements) != 4:
                print(f"[DrasaOnlineMapServer] tier spender shuffle unavailable for {offer_slot}")
                return
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            print(f"[DrasaOnlineMapServer] tier spender shuffle failed safely: {exc}")
            return

        commands = []
        with s.flow_lock:
            currency = s.gameplay_currency_state
            if (not s.connected or getattr(s, "retired", False)
                    or s.gameplay_exit_transition_started
                    or s.gameplay_daily_challenges != before
                    or currency is None or int(currency.currency_type) < cost):
                return
            spender = list(before.spender)
            spender[offset:offset + 4] = replacements
            state = replace(
                before,
                spender=tuple(spender),
                spender_progress=min(
                    0xffffffff, int(before.spender_progress) + int(cost)
                ),
            )
            currency = replace(
                currency, currency_type=int(currency.currency_type) - cost,
                notify=True,
            )
            s.gameplay_daily_challenges = state
            s.gameplay_tier_spender_challenges = state.spender
            s.gameplay_currency_state = currency
            # Publish the replacement map before the progress update. The
            # client refreshes the spender widget when 0x013a is handled, so
            # sending it first makes that refresh run against the old offers.
            commands.extend((
                ServerCommand(CURRENCY_CHANGED_COMMAND, currency, LOCAL_PLAYER_ACTOR_ID),
                ServerCommand(
                    TIMED_CHALLENGES_INFO_COMMAND,
                    # A shuffle changes only the RC spender map. Keep the
                    # unrelated daily and bonus maps out of this live update.
                    TimedChallengesInfo(
                        0, (), (), state.spender, (),
                    ),
                    LOCAL_PLAYER_ACTOR_ID,
                ),
                ServerCommand(
                    UPDATE_PROGRESS_TIER_SPENDER_CHALLENGES_COMMAND,
                    UpdateProgressTierSpenderChallenges(cost), 0,
                ),
            ))

        CharacterService._accept_gameplay_state_command(
            s, SHUFFLE_TIER_SPENDER_DEALS_COMMAND
        )
        CharacterService._request_account_save(
            s, f"Tier spender {offer_slot} shuffle", flush=True
        )
        s.send_reliable_ordered(
            encode_server_command_batch(tuple(commands), include_terminator=True),
            track=True, label="tier-spender-shuffle",
        )
        self._award_due_tier_spender_rewards(s)
        print(
            f"[DrasaOnlineMapServer] shuffled tier spender {offer_slot} "
            f"(button price tier {price_slot}) for "
            f"character {getattr(s.gameplay_character, 'character_id', '?')} ({cost} RC)"
        )

    def _daily_challenge_feed(self, s, kind, *, value='', count=1):
        """Track only accepted tasks from successful, server-owned actions.

        Progress and rewards are committed together under the session lock.
        A full inventory retains capped progress for retry, never discards a
        reward or opens the next tier prematurely.
        """
        self._season_pass_feed(s, kind, value=value, count=count)
        if (getattr(s, 'gameplay_daily_challenges', None) is None
                or s.gameplay_character is None or s.gameplay_exit_transition_started):
            return
        catalog, now, facts = self._daily_challenge_context(s)
        spender = self._tier_spender_offers(s, now, facts)
        if not any(e.state == ACCEPTED and (catalog.definitions[e.challenge_id].objective == kind
                   or e.progress >= catalog.definitions[e.challenge_id].target)
                   for e in s.gameplay_daily_challenges.daily) and not any(
                       e.state == ACCEPTED and e.progress >= catalog.definitions[e.challenge_id].target
                       for e in s.gameplay_daily_challenges.bonuses):
            return
        from dso_server.protocol.consumable_protocol import load_consumable_catalog
        effects = load_consumable_catalog(self.static_database_path)
        commands, applied = [], []
        initial_honor = int(s.gameplay_honor_points)
        with s.flow_lock:
            if not s.connected or getattr(s, 'retired', False) or s.gameplay_exit_transition_started:
                return
            before = s.gameplay_daily_challenges
            state = catalog.board(before, now.date(), facts)
            if state is None:
                return
            if len(state.spender) != 12:
                state = replace(state, spender=spender)
            entries = list(state.daily)
            difficulty = max(s.gameplay_map_difficulty, s.gameplay_scaling_difficulty)
            for index, entry in enumerate(entries):
                if entry.state != ACCEPTED:
                    continue
                definition = catalog.definitions[entry.challenge_id]
                amount = definition.increment(kind, value=value, count=count, difficulty=difficulty)
                progress = min(definition.target, entry.progress + amount)
                entry = replace(entry, progress=progress)
                if progress >= definition.target:
                    try:
                        reward_commands, reward_effect = self._daily_challenge_reward(
                            s, definition, effects)
                    except (ValueError, OSError) as exc:
                        print(f'[DrasaOnlineMapServer] daily reward pending {definition.resource_id}: {exc}')
                    else:
                        entry = replace(entry, state=COMPLETED, timestamp=(now.year, now.month, now.day, now.hour, now.minute, now.second))
                        commands.extend(reward_commands)
                        if reward_effect:
                            applied.append(reward_effect)
                entries[index] = entry
            completed = sum(e.state == COMPLETED for e in entries)
            bonuses = list(state.bonuses)
            for index, entry in enumerate(bonuses):
                if entry.state == COMPLETED:
                    continue
                definition = catalog.definitions[entry.challenge_id]
                progress = min(definition.target, completed)
                entry = replace(entry, progress=progress)
                if progress >= definition.target:
                    try:
                        reward_commands, reward_effect = self._daily_challenge_reward(s, definition, effects)
                    except (ValueError, OSError) as exc:
                        print(f'[DrasaOnlineMapServer] daily bonus pending {definition.resource_id}: {exc}')
                    else:
                        entry = replace(entry, state=COMPLETED, timestamp=(now.year, now.month, now.day, now.hour, now.minute, now.second))
                        commands.extend(reward_commands)
                        if reward_effect:
                            applied.append(reward_effect)
                bonuses[index] = entry
            state = catalog.board(replace(state, daily=tuple(entries), bonuses=tuple(bonuses)), now.date(), facts)
            s.gameplay_daily_challenges = state
            if state == before and not commands:
                return
            # Reward commands (items, currency) must stay ordered with every
            # other inventory change on channel 0; pure progress may not.
            rewarded = bool(commands)
            old_entries = {e.challenge_id: e for e in (*before.daily, *before.bonuses)}
            commands.extend(ServerCommand(DAILY_CHALLENGE_PROGRESS_COMMAND, entry, LOCAL_PLAYER_ACTOR_ID)
                            for entry in (*state.daily, *state.bonuses)
                            if entry.challenge_id in old_entries and entry != old_entries[entry.challenge_id])
            commands.append(ServerCommand(TIMED_CHALLENGES_INFO_COMMAND, state.info(), LOCAL_PLAYER_ACTOR_ID))
        s.send_reliable_ordered(encode_server_command_batch(tuple(commands), include_terminator=True),
                                track=True,
                                label='daily-challenge-rewards' if rewarded else 'daily-challenge-progress')
        if s.gameplay_honor_points != initial_honor:
            from dso_server.protocol.pvp_match_protocol import HonorPointsChanged, encode_honor_points_changed
            s.send_reliable_ordered(encode_honor_points_changed(HonorPointsChanged(
                s.gameplay_honor_points, LOCAL_PLAYER_ACTOR_ID)), track=True, label='daily-challenge-honor')
        for entry, definition, retired, timers in applied:
            for timer in timers:
                timer.cancel()
            CharacterService._schedule_consumable_timers(s, entry.field_04, definition,
                                                          database_path=self.static_database_path)
            if s.gameplay_presence_callback:
                from dso_server.protocol.gameplay_protocol import StatusEffect
                s.gameplay_presence_callback(s, ('status_effects', (StatusEffect(False, (*retired, entry)),)))
        if applied:
            CharacterService._refresh_player_combat_vitals(s)
        newly_completed = sum(
            entry.state == COMPLETED
            and old_entries.get(entry.challenge_id) is not None
            and old_entries[entry.challenge_id].state != COMPLETED
            for entry in (*state.daily, *state.bonuses)
        )
        CharacterService._request_account_save(s, 'Daily challenge progress and rewards',
                                               flush=bool(newly_completed))
        for _ in range(newly_completed):
            self._season_pass_feed(s, 'DailyChallengeClosed')

    def _daily_challenge_reward(self, s, definition, effects):
        """Stage inventory/currency before committing a completed task's payout."""
        from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder
        from dso_server.protocol.commerce_protocol import load_purchase_item_definition
        from dso_server.protocol.item_receipt_protocol import (
            ITEM_RECEIVED_TEMPLATE_CATEGORIES, ItemReceiptGrant, build_item_receipts,
            item_received_message,
        )
        from dso_server.protocol.player_init_protocol import INVENTORY_INFO_COMMAND, MESSAGE_COMMAND
        from dso_server.protocol.gameplay_protocol import STATUS_EFFECT_COMMAND, StatusEffect
        import time
        currency = s.gameplay_currency_state
        honor = int(s.gameplay_honor_points)
        builder = None
        receipt_grants = []
        effect_definition = None
        if definition.reward_effect:
            resource = definition.reward_effect.split(',', 1)[0]
            effect_definition = next((d for values in effects.definitions_by_effect_id.values()
                                      for d in values if d.effect_resource_id == resource), None)
            if effect_definition is None:
                raise ValueError('unknown challenge reward effect ' + resource)
        for reward in definition.reward.split(';'):
            if not reward:
                continue
            kind, _, raw = reward.partition(':')
            fields = raw.split(',')
            if kind == 'Loot':
                if s.gameplay_inventory is None:
                    raise ValueError('inventory not initialized')
                builder = builder or InventoryMutationBuilder(s.gameplay_inventory, s.gameplay_next_item_id)
                item = load_purchase_item_definition(self.static_database_path, fields[0])
                amount = int(fields[1]) if len(fields) > 1 else 1
                builder.add_item(item, amount, item_level=s.gameplay_character.level)
                receipt_grants.append(ItemReceiptGrant(
                    item.template_id, amount,
                    include_template_id=item.item_category in ITEM_RECEIVED_TEMPLATE_CATEGORIES,
                ))
            else:
                amount = int(fields[0])
                if 'CharacterLevel' in fields[1:]:
                    amount *= int(s.gameplay_character.level)
                if kind == 'VC':
                    if currency is None:
                        raise ValueError('currency not initialized')
                    currency = replace(currency, amount=currency.amount + amount, notify=True)
                elif kind == 'HC':
                    honor += amount
                else:
                    raise ValueError('unknown reward kind ' + kind)
        mutation = builder.finish() if builder else None
        receipts = ()
        if mutation:
            # Before anything is committed: a failure leaves the reward pending.
            receipts, _ = build_item_receipts(
                s.gameplay_inventory, mutation.inventory,
                (entry.item_id for entry in mutation.changed_items),
                mutation.replacements, receipt_grants,
                next_item_id=mutation.next_item_id,
            )
        commands = []
        applied = None
        if effect_definition:
            installed = CharacterService._install_instant_status_entry(
                s, effect_definition, effects, server_tick=s.gameplay_chase_server_tick,
                now=time.perf_counter(), now_epoch=time.time())
            if installed:
                entry, retired, timers, _ = installed
                commands.append(ServerCommand(STATUS_EFFECT_COMMAND, StatusEffect(False, (*retired, entry)),
                                              LOCAL_PLAYER_ACTOR_ID))
                applied = (entry, effect_definition, retired, timers)
        if currency != s.gameplay_currency_state:
            s.gameplay_currency_state = currency
            commands.append(ServerCommand(CURRENCY_CHANGED_COMMAND, s.gameplay_currency_state, LOCAL_PLAYER_ACTOR_ID))
        if honor != s.gameplay_honor_points:
            s.gameplay_honor_points = honor
        if mutation:
            s.gameplay_inventory = mutation.inventory
            s.gameplay_next_item_id = mutation.next_item_id
            commands.extend((ServerCommand(INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID),
                             *(ServerCommand(MESSAGE_COMMAND, item_received_message(receipt),
                                             LOCAL_PLAYER_ACTOR_ID) for receipt in receipts),
                             ServerCommand(INVENTORY_INFO_COMMAND, mutation.final, LOCAL_PLAYER_ACTOR_ID)))
        return commands, applied

    def _daily_challenge_monsters_killed(self, s, monsters):
        from collections import Counter
        monsters = tuple(monsters)
        counts = Counter()
        templates = Counter()
        for monster in monsters:
            counts['MonstersKilled'] += 1
            rank = str(getattr(monster, 'rank_id', getattr(monster, 'rank', ''))).casefold()
            if 'champion' in rank or 'leader' in rank:
                counts['ChampionsAndLeadersKilled'] += 1
            if 'sentinel' in rank:
                counts['SentinelKilled'] += 1
            if 'boss' in rank:
                counts['BossesKilled'] += 1
            templates[monster.template_id] += 1
        for objective, count in counts.items():
            self._daily_challenge_feed(s, objective, count=count)
        for template, count in templates.items():
            self._daily_challenge_feed(s, 'SpecificMonsterKilled', value=template, count=count)
