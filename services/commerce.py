"""NPC/OCP offers, rewards, and purchase transactions."""

from __future__ import annotations

from dataclasses import replace

from dso_server.runtime.context import *
from dso_server.runtime.admin_commands import track_inventory_reward
from dso_server.runtime.transport import Session
from dso_server.services.events import EVENT_NOTIFICATION_KIND
from dso_server.protocol.shop_protocol import OFFER_COMMAND


def _transaction_touches_npc_gates(template_ids) -> bool:
    """Whether trading these items can change what NPCInfo shows.

    NPC offers and markers follow the quest log and map events, which a
    vendor trade never changes; the only inventory input is a gate key such
    as ``key_from_f4102_to_f4201`` (``_quest_reveal_flags``). Rebuilding
    every NPC's quest list after each buy cost end-game characters a quarter
    second per click, since it scales with the size of the quest log.
    """

    return any(
        str(template_id).casefold().startswith("key_")
        for template_id in template_ids
    )


class CommerceService:
    """NPC/OCP offers, rewards, and purchase transactions."""

    def _build_shop_offer_snapshot(self, s: Session, shop_id: str):
        """Build the same filtered offer snapshot used for a fresh shop open."""
        character = s.gameplay_character
        if character is None and hasattr(self, 'character_selection'):
            character = self.character_selection.active_character
        player_level = max(1, int(getattr(character, 'level', 1)))
        class_id = int(getattr(character, 'class_id', 0))
        offers = load_shop_offers(
            self.static_database_path,
            shop_id,
            player_level=player_level,
            class_id=class_id,
            active_event_ids=CharacterService._event_catalog(
                self
            ).active_template_ids(),
        )
        with s.flow_lock:
            owned_stickers = tuple(s.gameplay_stickerbook_entries)
            vendor_buyback = tuple(s.gameplay_vendor_buyback)
        hidden_item_ids = owned_source_item_ids(
            self.static_database_path,
            owned_stickers,
        )
        if hidden_item_ids:
            visible_offers = tuple(
                offer
                for offer in offers.offers
                if offer is None or offer.item_id not in hidden_item_ids
            )
            visible_categories = tuple(dict.fromkeys(
                offer.category for offer in visible_offers if offer is not None
            ))
            offers = replace(
                offers,
                offers=visible_offers,
                categories=visible_categories,
            )
        return replace(offers, sold_items=tuple(
            copy.deepcopy(item) for item, _ in vendor_buyback
        ))

    def _refresh_open_npc_shop(self, s: Session, shop_id: str) -> bool:
        """Publish a CMS shop change to a player who currently has it open."""
        if not self.is_session_current(s):
            return False
        with s.flow_lock:
            if s.gameplay_open_shop_id != shop_id:
                return False
            actor_id = s.gameplay_open_shop_actor_id
        offers = replace(
            self._build_shop_offer_snapshot(s, shop_id),
            field_60=True,
        )
        with s.flow_lock:
            if s.gameplay_open_shop_id != shop_id:
                return False
            s.gameplay_open_shop_offers = offers
        packet, packet_bits = encode_offer_response_batch(
            offers,
            timed_offers_actor_id=LOCAL_PLAYER_ACTOR_ID,
            offer_actor_id=int(actor_id or 0),
        )
        s.send_reliable_ordered_split(
            packet,
            total_bit_length=packet_bits,
            pace_seconds=0.002,
            track=True,
            label_prefix='admin-npc-shop-refresh',
        )
        return True

    def _handle_request_offer(self, s: Session, payload: bytes) -> None:
        request = decode_request_offer(payload)
        CharacterService._accept_gameplay_state_command(s, 0x0059)
        with s.flow_lock:
            s.gameplay_client_command_state[0x0059] = request

        # Resolve against the same map-local event set and actor aliases as
        # NPCRequest; global event flags alone can disagree with the NPC the
        # player just opened (including the permanent fairground vendors).
        definition = CharacterService._resolve_npc_interaction(
            self, s, request.npc_actor_id
        )
        with s.flow_lock:
            fallback_shop_id = s.gameplay_interaction_npc_shop_id
        shop_id = request.offer_id.strip() or (
            definition.shop_id if definition is not None else None
        ) or (fallback_shop_id if int(request.npc_actor_id) == 0 else None)
        if not shop_id:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] RequestOffer references NPC actor "
                f"{request.npc_actor_id}, which has no local shop"
            )
            return
        if (
            shop_id == WISDOM_TALENT_SHOP_ID
            and int(request.npc_actor_id) == 0
            and CharacterService._active_talent_rank(
                s, 1, WISDOM_TALENT_SHOP_TALENT
            ) <= 0
        ):
            # The Skills & Talents window opens this trader from anywhere,
            # but only with the Wisdom trait that unlocks it.
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] RequestOffer for "
                f"{shop_id!r} needs the {WISDOM_TALENT_SHOP_TALENT!r} "
                "Wisdom talent"
            )
            return

        offers = self._build_shop_offer_snapshot(s, shop_id)
        with s.flow_lock:
            s.gameplay_open_shop_id = shop_id
            s.gameplay_open_shop_actor_id = request.npc_actor_id
            s.gameplay_open_shop_offers = offers
        packet, packet_bits = encode_offer_response_batch(
            offers,
            timed_offers_actor_id=LOCAL_PLAYER_ACTOR_ID,
            # OfferCommand is actor-scoped. For an NPC shop, the client
            # requested this shop against that NPC actor and expects the
            # response to retain the same actor context. Global-store
            # requests use actor zero and remain actorless.
            offer_actor_id=int(request.npc_actor_id),
        )
        s.send_reliable_ordered_split(
            packet,
            total_bit_length=packet_bits,
            pace_seconds=0.002,
            track=True,
            label_prefix='npc-shop-offers-npc-shop-offers',
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] handled RequestOffer for NPC actor "
            f"{request.npc_actor_id}: sent {len(offers.offers)} typed "
            f"{shop_id!r} offers from static.db4 "
            f"({len(packet)} bytes/{packet_bits} bits; "
            f"offer_actor={int(request.npc_actor_id)})"
        )

    @track_inventory_reward
    def _deliver_rewards(
            self, s: Session, definitions: tuple[RewardDefinition, ...],
            *, claim_keys: tuple[str, ...] | None, label: str,
            claim_value: int = 1,
            enchant_equipment: bool = False) -> None:
        # Keep the database dependency explicit instead of retaining any
        # capture-derived inventory objects.
        with s.flow_lock:
            inventory = copy.deepcopy(s.gameplay_inventory)
            currency = copy.deepcopy(s.gameplay_currency_state)
            next_item_id = int(s.gameplay_next_item_id)
            claims = dict(s.gameplay_timed_offer_purchases)
            character = copy.deepcopy(s.gameplay_character)
        if inventory is None:
            raise ValueError("player inventory is not initialized")
        inventory_before = copy.deepcopy(inventory)
        if (
            claim_keys is not None
            and any(claims.get(key, 0) for key in claim_keys)
        ):
            raise ValueError("one or more rewards were already claimed")
        changed_items = []
        currency_changed = False
        replaced_item_ids = []
        replacements = {}
        receipt_grants = []
        for definition in definitions:
            if definition.item_id in {'money_rc', 'money_vc', 'money_vc_gold'}:
                if currency is None:
                    raise ValueError('player currency state is not initialized')
                if definition.item_id == 'money_rc':
                    field = 'currency_type'
                    currency_name = 'Andermant'
                else:
                    field = 'amount'
                    currency_name = 'gold'
                balance = int(getattr(currency, field)) + int(definition.amount)
                if balance > 0xFFFFFFFF:
                    raise ValueError(
                        f'{currency_name} balance exceeds wire range'
                    )
                currency = replace(currency, **{field: balance}, notify=True)
                currency_changed = True
                continue
            item = load_purchase_item_definition(
                self.static_database_path, definition.item_id
            )
            receipt_grants.append(
                ItemReceiptGrant(
                    definition.item_id,
                    definition.amount,
                    materialized=True,
                    include_template_id=(
                        item.item_category
                        in ITEM_RECEIVED_TEMPLATE_CATEGORIES
                    ),
                )
            )
            mutation = add_purchased_item(
                inventory,
                item,
                definition.amount,
                next_item_id=next_item_id,
                item_level=max(
                    1, int(getattr(character, 'level', 1))
                ),
            )
            next_item_id = mutation.next_item_id
            changed_items.extend(mutation.changed_items)
            replaced_item_ids.extend(mutation.replaced_item_ids)
            replacements.update(mutation.replacements)
        if enchant_equipment and changed_items:
            changed_item_ids = tuple(
                int(item.item_id) for item in changed_items
            )
            roll_new_equipment_enchantments(
                self.static_database_path,
                inventory,
                changed_item_ids,
                class_name=CLASS_NAMES[int(character.class_id)].lower(),
                map_difficulty=int(s.gameplay_map_difficulty),
            )
            refresh_inventory_economy_values(
                self.static_database_path,
                inventory,
                item_ids=changed_item_ids,
                force_melt_value=True,
            )
            items_by_id = {
                int(item.item_id): item for item in inventory.items
            }
            changed_items = [
                items_by_id[item_id] for item_id in changed_item_ids
            ]
        delta = copy.deepcopy(inventory)
        delta.items = copy.deepcopy(changed_items)
        delta.changed_item_ids = list(replaced_item_ids)
        delta.field_158 = dict(replacements)
        delta.field_1e0 = True
        final = copy.deepcopy(inventory)
        final.items = []
        final.changed_item_ids = []
        final.field_158 = {}
        final.field_1e0 = False
        receipts, next_item_id = build_item_receipts(
            inventory_before,
            inventory,
            (item.item_id for item in changed_items),
            replacements,
            receipt_grants,
            next_item_id=next_item_id,
        )
        commands = []
        if currency_changed:
            commands.append(ServerCommand(
                CURRENCY_CHANGED_COMMAND, currency, LOCAL_PLAYER_ACTOR_ID
            ))
        commands.append(ServerCommand(
            INVENTORY_INFO_COMMAND, delta, LOCAL_PLAYER_ACTOR_ID
        ))
        commands.extend(
            ServerCommand(
                MESSAGE_COMMAND,
                item_received_message(receipt),
                LOCAL_PLAYER_ACTOR_ID,
            )
            for receipt in receipts
        )
        commands.append(
            ServerCommand(
                INVENTORY_INFO_COMMAND, final, LOCAL_PLAYER_ACTOR_ID
            )
        )
        packet, packet_bits = encode_server_command_batch(
            tuple(commands),
            include_terminator=True,
        )
        with s.flow_lock:
            s.gameplay_inventory = inventory
            if currency_changed:
                s.gameplay_currency_state = currency
            s.gameplay_next_item_id = next_item_id
            for key in claim_keys or ():
                claims[key] = int(claim_value)
            s.gameplay_timed_offer_purchases = claims
        s.send_reliable_ordered_split(
            packet,
            total_bit_length=packet_bits,
            track=True,
            label_prefix=f'{label}-{label}',
        )
        CharacterService._request_account_save(
            s, f"{label} claimed", flush=True
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] granted {label}: "
            f"{[(entry.item_id, entry.amount) for entry in definitions]}"
        )

    def _grant_inventory_item(
            self, s: Session, template_id: str, amount: int) -> None:
        """Grant a repeatable admin item through the normal inventory path."""

        amount = int(amount)
        if not 1 <= amount <= 1000:
            raise ValueError("item amount must be between 1 and 1000")
        self._deliver_rewards(
            s,
            (
                RewardDefinition(
                    "admin-item",
                    str(template_id),
                    amount,
                ),
            ),
            claim_keys=None,
            label="admin-item",
            enchant_equipment=True,
        )

    def _handle_ocp_offer_request(self, s: Session, payload: bytes) -> None:
        self._handle_simple_gameplay_command(s, payload, 0x010B)
        packet = encode_ocp_offer_batch(
            OCPOfferBatch(
                LOCAL_OCP_OFFERS,
                0,
                LOCAL_OCP_METHODS,
                0,
            )
        )
        s.send_reliable_ordered_split(
            packet[0],
            total_bit_length=packet[1],
            track=True,
            label_prefix='ocp-offers-ocp-offers',
        )

    @staticmethod
    def _ocp_booking_notification(instance_guid, reward):
        return make_notification_entry(
            OCP_BOOKING_NOTIFICATION_KIND,
            instance_guid,
            OCP_BOOKING_NOTIFICATION_VALUES,
            ocp_booking_notification_payload(reward, instance_guid),
        )

    def _send_ocp_booking_notification(
            self, s: Session, instance_guid: bytes, reward) -> None:
        packet, packet_bits = encode_server_command_batch(
            (
                ServerCommand(
                    CLIENT_NOTIFICATION_COMMAND,
                    Notification(
                        1,
                        (self._ocp_booking_notification(
                            instance_guid, reward
                        ),),
                    ),
                    0,
                ),
            ),
            include_terminator=True,
        )
        s.send_reliable_ordered_split(
            packet,
            total_bit_length=packet_bits,
            track=True,
            label_prefix=(
                'ocp-booking-notification-'
                'ocp-booking-notification'
            ),
        )

    def _handle_ocp_booking(self, s: Session, payload: bytes) -> None:
        request = decode_ocp_booking_request(payload)
        reward = resolve_ocp_booking_reward(
            request.offer_id, request.payment_method
        )
        instance_guid = uuid.uuid4().bytes
        pending_key = ocp_booking_pending_key(
            instance_guid, reward.offer_id
        )
        with s.flow_lock:
            claims = dict(s.gameplay_timed_offer_purchases)
            claims[pending_key] = 1
            s.gameplay_timed_offer_purchases = claims
        CharacterService._request_account_save(
            s, 'OCP booking created', flush=True
        )
        self._send_ocp_booking_notification(s, instance_guid, reward)
        CharacterService._accept_gameplay_state_command(
            s, OCP_BOOKING_COMMAND
        )
        print(
            f'[{GAMEPLAY_SERVICE_NAME}] created pending OCP booking '
            f'{instance_guid.hex()} for {reward.amount}x '
            f'{reward.item_id}; awaiting notification confirmation'
        )

    def _handle_tier_spender_accept(
            self, s: Session, payload: bytes) -> None:
        reward_ids = decode_tier_spender_accept(payload)
        definitions = load_spender_rewards(self.static_database_path, reward_ids)
        by_id = {definition.claim_id: definition for definition in definitions}
        today = self._daily_challenge_now().date().isoformat()
        with s.flow_lock:
            state = s.gameplay_daily_challenges
            if (state is None or state.day != today
                    or len(state.spender) != 12):
                raise ValueError("tier spender board is unavailable")
            groups = tuple(
                {entry.challenge_id for entry in state.spender[index:index + 4]}
                for index in (0, 4, 8)
            )
            selected_by_group = []
            remaining = set(reward_ids)
            for group in groups:
                matches = remaining.intersection(group)
                if len(matches) != 1:
                    raise ValueError(
                        "tier spender selection must contain one reward per tier"
                    )
                selected = next(iter(matches))
                selected_by_group.append(selected)
                remaining.remove(selected)
            if remaining or len(reward_ids) != 3:
                raise ValueError(
                    "tier spender selection must contain one reward per tier"
                )
            selections = tuple(selected_by_group)
            if state.spender_selections and state.spender_selections != selections:
                raise ValueError("tier spender rewards were already selected")
            if state.spender_selections != selections:
                s.gameplay_daily_challenges = replace(
                    state, spender_selections=selections
                )
        if state.spender_selections != selections:
            CharacterService._request_account_save(
                s, "Tier spender rewards selected", flush=True
            )
        CharacterService._accept_gameplay_state_command(
            s, ACCEPTED_TIER_SPENDER_COMMAND
        )
        self._award_due_tier_spender_rewards(s, by_id=by_id)

    def _award_due_tier_spender_rewards(self, s, *, by_id=None):
        with s.flow_lock:
            state = s.gameplay_daily_challenges
            if state is None or not state.spender_selections:
                return
            progress = int(state.spender_progress)
            claims = dict(s.gameplay_timed_offer_purchases)
            selected = tuple(state.spender_selections)
        if by_id is None:
            definitions = load_spender_rewards(
                self.static_database_path, selected
            )
            by_id = {definition.claim_id: definition for definition in definitions}
        due = tuple(
            by_id[reward_id] for reward_id in selected
            if reward_id in by_id
            and progress >= int(by_id[reward_id].rc_needed)
            and not claims.get(f"spender:{reward_id}", 0)
        )
        if due:
            self._deliver_rewards(
                s, due,
                claim_keys=tuple(f"spender:{item.claim_id}" for item in due),
                label="tier-spender-reward",
            )

    def _credit_tier_spender_progress(self, s, amount):
        amount = int(amount)
        if amount <= 0:
            return
        today = self._daily_challenge_now().date().isoformat()
        with s.flow_lock:
            state = s.gameplay_daily_challenges
            if state is None or state.day != today or len(state.spender) != 12:
                return
            progress = min(0xffffffff, int(state.spender_progress) + amount)
            if progress == state.spender_progress:
                return
            s.gameplay_daily_challenges = replace(
                state, spender_progress=progress
            )
        CharacterService._request_account_save(
            s, "Tier spender progress", flush=True
        )
        self._award_due_tier_spender_rewards(s)

    def _settle_ocp_booking(
            self, s: Session, pending_key: str, reward) -> None:
        with s.flow_lock:
            claims = dict(s.gameplay_timed_offer_purchases)
            if not claims.get(pending_key, 0):
                return
            inventory = copy.deepcopy(s.gameplay_inventory)
            currency = s.gameplay_currency_state
            next_item_id = int(s.gameplay_next_item_id)
            character = copy.deepcopy(s.gameplay_character)
            commands = []
            if reward.item_id == 'money_rc':
                if currency is None:
                    raise ValueError('player currency state is not initialized')
                balance = int(currency.currency_type) + reward.amount
                if balance > 0xFFFFFFFF:
                    raise ValueError('Andermant balance exceeds wire range')
                currency = CurrencyChanged(
                    balance, currency.amount, currency.values,
                    currency.delta, True, currency.wallet_base,
                )
                commands.append(ServerCommand(
                    CURRENCY_CHANGED_COMMAND, currency,
                    LOCAL_PLAYER_ACTOR_ID,
                ))
            elif reward.item_id in {'money_vc', 'money_vc_gold'}:
                if currency is None:
                    raise ValueError('player currency state is not initialized')
                balance = int(currency.amount) + reward.amount
                if balance > 0xFFFFFFFF:
                    raise ValueError('gold balance exceeds wire range')
                currency = CurrencyChanged(
                    currency.currency_type, balance, currency.values,
                    currency.delta, True, currency.wallet_base,
                )
                commands.append(ServerCommand(
                    CURRENCY_CHANGED_COMMAND, currency,
                    LOCAL_PLAYER_ACTOR_ID,
                ))
            else:
                if inventory is None:
                    raise ValueError('player inventory is not initialized')
                item = load_purchase_item_definition(
                    self.static_database_path, reward.item_id
                )
                if item.item_category == 'Currency':
                    raise ValueError(
                        f'unsupported wallet currency {reward.item_id!r}'
                    )
                mutation = add_purchased_item(
                    inventory, item, reward.amount,
                    next_item_id=next_item_id,
                    item_level=max(
                        1, int(getattr(character, 'level', 1))
                    ),
                )
                next_item_id = mutation.next_item_id
                commands.extend((
                    ServerCommand(
                        INVENTORY_INFO_COMMAND, mutation.delta,
                        LOCAL_PLAYER_ACTOR_ID,
                    ),
                    ServerCommand(
                        INVENTORY_INFO_COMMAND, mutation.final,
                        LOCAL_PLAYER_ACTOR_ID,
                    ),
                ))
            parsed = parse_ocp_booking_pending_key(pending_key)
            if parsed is None:
                raise ValueError('invalid pending OCP booking key')
            claims.pop(pending_key, None)
            claims[ocp_booking_completed_key(parsed[0])] = 1
            packet, packet_bits = encode_server_command_batch(
                tuple(commands), include_terminator=True
            )
            s.gameplay_currency_state = currency
            s.gameplay_inventory = inventory
            s.gameplay_next_item_id = next_item_id
            s.gameplay_timed_offer_purchases = claims
        CharacterService._request_account_save(
            s, 'OCP booking settled', flush=True
        )
        s.send_reliable_ordered_split(
            packet,
            total_bit_length=packet_bits,
            track=True,
            label_prefix=(
                'ocp-booking-settlement-'
                'ocp-booking-settlement'
            ),
        )
        print(
            f'[{GAMEPLAY_SERVICE_NAME}] settled OCP booking: '
            f'{reward.amount}x {reward.item_id}'
        )

    def _handle_notification_action(
            self, s: Session, payload: bytes) -> None:
        action = decode_notification_action(payload)
        if action.kind == OCP_BOOKING_NOTIFICATION_KIND:
            # CMS grants are already applied. Their receipt-only acknowledgement
            # must never enter the premium-store reward settlement path.
            from dso_server.services.admin_live import BOOKING_GUID_PREFIX
            if len(action.instance_guid) == 16 and action.instance_guid.startswith(BOOKING_GUID_PREFIX):
                booking_id = int.from_bytes(action.instance_guid[8:], "big")
                with s.flow_lock:
                    delivered = booking_id in s.gameplay_admin_booking_ids
                if not delivered:
                    raise ValueError("unknown CMS booking notification")
                CharacterService._accept_gameplay_state_command(s, CLIENT_NOTIFICATION_COMMAND)
                return
            completed_key = ocp_booking_completed_key(
                action.instance_guid
            )
            with s.flow_lock:
                claims = dict(s.gameplay_timed_offer_purchases)
            pending = next(
                (
                    (key, parsed[1])
                    for key in claims
                    if (
                        (parsed := parse_ocp_booking_pending_key(key))
                        is not None
                        and parsed[0] == action.instance_guid
                    )
                ),
                None,
            )
            if pending is None:
                if claims.get(completed_key, 0):
                    CharacterService._accept_gameplay_state_command(
                        s, CLIENT_NOTIFICATION_COMMAND
                    )
                    print(
                        f'[{GAMEPLAY_SERVICE_NAME}] ignored repeated OCP '
                        f'booking confirmation {action.instance_guid.hex()}'
                    )
                    return
                raise ValueError('unknown OCP booking notification')
            pending_key, offer_id = pending
            reward = resolve_ocp_booking_reward(offer_id, 'external')
            expected_payload = ocp_booking_notification_payload(
                reward, action.instance_guid
            )
            if action.values != OCP_BOOKING_NOTIFICATION_VALUES:
                raise ValueError('OCP booking notification values mismatch')
            for key, expected in expected_payload.items():
                if action.payload.get(key) != expected:
                    raise ValueError(
                        f'OCP booking notification field {key!r} mismatch'
                    )
            if action.subtype not in (1, 2):
                raise ValueError(
                    f'unsupported OCP booking action {action.subtype}'
                )
            if action.subtype == 1:
                self._settle_ocp_booking(s, pending_key, reward)
            else:
                with s.flow_lock:
                    claims = dict(s.gameplay_timed_offer_purchases)
                    claims.pop(pending_key, None)
                    claims[completed_key] = 1
                    s.gameplay_timed_offer_purchases = claims
                CharacterService._request_account_save(
                    s, 'OCP booking declined', flush=True
                )
            CharacterService._accept_gameplay_state_command(
                s, CLIENT_NOTIFICATION_COMMAND
            )
            return
        invitation_kind = str(action.payload.get("invitation", ""))
        if invitation_kind in {"group", "guild", "group-request"}:
            # The echoed payload is client-controlled; social invitations are
            # only completed through the social store's invitation ledger.
            CharacterService._handle_social_notification_action(self, s, action)
            return
        if "dailyLoginCount" not in action.payload:
            if (
                "eventId" in action.payload
                or action.kind == EVENT_NOTIFICATION_KIND
            ):
                # An event announcement has no server-side effect yet. The
                # one candidate, ButtonTeleport on a template with a
                # TeleportTarget, waits for a capture of what the client
                # sends; the active template has neither. Acknowledge so an
                # unknown reply never desyncs the session state machine.
                CharacterService._accept_gameplay_state_command(
                    s, CLIENT_NOTIFICATION_COMMAND
                )
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] event notification "
                    f"{action.instance_guid.hex()} answered with subtype "
                    f"{action.subtype} (event "
                    f"{action.payload.get('eventId')}, template "
                    f"{action.payload.get('templateId')!r})"
                )
                return
            raise ValueError("unsupported notification action")
        if not DAILY_LOGIN_REWARDS_ENABLED:
            CharacterService._accept_gameplay_state_command(
                s, CLIENT_NOTIFICATION_COMMAND
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] ignored disabled daily-login "
                "reward action"
            )
            return
        today_date = datetime.now(timezone.utc).date()
        today = today_date.isoformat()
        with s.flow_lock:
            claims = dict(s.gameplay_timed_offer_purchases)
            character = s.gameplay_character
            premium_active = bool(s.gameplay_membership_active)
        day = pending_daily_login_day(claims, today=today_date)
        if day is None:
            raise ValueError("daily login reward was already claimed today")
        if character is None:
            raise ValueError("daily login character is not initialized")
        class_name = CLASS_NAMES[int(character.class_id)].lower()
        expected_reward = daily_login_reward_group(class_name, day)
        expected_premium = premium_daily_login_reward_group(class_name, day)
        reward_id = str(action.payload.get("reward", ""))
        premium_reward_id = str(action.payload.get("premiumReward", ""))
        if (
            reward_id != expected_reward
            or premium_reward_id != expected_premium
            or int(action.payload.get("dailyLoginCount", 0)) != day
            or int(action.payload.get("rewardIndex", -1)) != day - 1
        ):
            raise ValueError("daily login notification does not match state")
        definitions = list(
            load_offer_group_rewards(
                self.static_database_path, reward_id
            )
        )
        # The premium reward is a second database offer group in the same
        # notification. Retail only grants it while membership is active.
        if premium_active:
            definitions.extend(
                load_offer_group_rewards(
                    self.static_database_path, premium_reward_id
                )
            )
        self._deliver_rewards(
            s,
            tuple(definitions),
            claim_keys=(f"daily-login:{today}",),
            label="daily-login-reward",
            claim_value=day,
        )
        CharacterService._accept_gameplay_state_command(
            s, CLIENT_NOTIFICATION_COMMAND
        )

    def _handle_activity_signin(
            self, s: Session, payload: bytes) -> None:
        """Return the durable sign-in calendar requested by the reward UI."""

        operation = decode_activity_signin_request(payload)
        CharacterService._accept_gameplay_state_command(
            s, ACTIVITY_SIGNIN_PROTOCOL_COMMAND
        )
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        current_key = f"activity-signin:{today}"
        with s.flow_lock:
            claims = dict(s.gameplay_timed_offer_purchases)
            # Operation 1 advances today's durable calendar state. Operation
            # 2 is the client's post-widget refresh and must never duplicate
            # the day's inventory rewards.
            current_sequence = int(claims.get(current_key, 0))
            changed = operation == 1 and current_sequence <= 0
            if changed:
                sequence = max(
                    (
                        int(value)
                        for key, value in claims.items()
                        if key.startswith("activity-signin:")
                    ),
                    default=0,
                ) + 1
            else:
                sequence = current_sequence

        if changed and DAILY_LOGIN_REWARDS_ENABLED:
            # This calendar is an independent 14-day bonus, not the classic
            # login-reward notification. In the fresh retail capture its
            # inventory delta precedes the updated calendar response.
            reward_day = (sequence - 1) % 14 + 1
            self._deliver_rewards(
                s,
                load_payment_signin_rewards(
                    self.static_database_path, reward_day
                ),
                claim_keys=(current_key,),
                label="activity-signin-reward",
                claim_value=sequence,
            )
            with s.flow_lock:
                claims = dict(s.gameplay_timed_offer_purchases)
        elif changed:
            with s.flow_lock:
                claims[current_key] = sequence
                s.gameplay_timed_offer_purchases = claims

        days = []
        for key, sequence in sorted(claims.items()):
            if not key.startswith("activity-signin:"):
                continue
            raw_date = key.removeprefix("activity-signin:")
            try:
                calendar_date = datetime.strptime(
                    raw_date, "%Y-%m-%d"
                ).date()
            except ValueError:
                continue
            value_time = (
                now.hour * 10000 + now.minute * 100 + now.second
                if calendar_date == now.date()
                else 0
            )
            days.append(
                ActivitySigninDay(
                    (
                        calendar_date.year * 10000
                        + calendar_date.month * 100
                        + calendar_date.day,
                        value_time,
                        0xFFFFFFFF,
                        int(sequence),
                    )
                )
            )
        calendar = ActivitySigninCalendar(tuple(days[-32:]))
        packet, packet_bits = encode_server_command_batch(
            (
                ServerCommand(
                    ACTIVITY_SIGNIN_CALENDAR_COMMAND,
                    calendar,
                    0,
                ),
            ),
            include_terminator=True,
        )
        s.send_reliable_ordered(
            (packet, packet_bits),
            track=True,
            label="activity-signin-calendar",
        )
        if changed and not DAILY_LOGIN_REWARDS_ENABLED:
            CharacterService._request_account_save(
                s, "activity sign-in recorded", flush=True
            )

    def _handle_transaction(self, s: Session, payload: bytes) -> None:
        transaction = decode_transaction(payload)
        CharacterService._accept_gameplay_state_command(
            s, TRANSACTION_COMMAND
        )
        CharacterService._sync_inventory_live_health(s)
        with s.flow_lock:
            s.gameplay_client_command_state[TRANSACTION_COMMAND] = transaction
            inventory = (
                None if s.gameplay_inventory is None
                else clone_inventory_info(s.gameplay_inventory)
            )
            currency = s.gameplay_currency_state
            game_currencies = dict(s.gameplay_game_currencies)
            next_item_id = s.gameplay_next_item_id
            open_shop_id = s.gameplay_open_shop_id
            open_offers = s.gameplay_open_shop_offers
            buyback = list(copy.deepcopy(s.gameplay_vendor_buyback))
            character = copy.deepcopy(s.gameplay_character)

        try:
            if not transaction.entries or any(
                entry is None for entry in transaction.entries
            ):
                raise ValueError("transaction has no concrete offer entries")
            if any(entry.offer_id.startswith("quickbar_") for entry in transaction.entries):
                # The bar's plus button buys a direct offer without opening
                # an NPC shop. Validate its tier and price independently.
                import sqlite3
                from dso_server.protocol.commerce_protocol import PurchasePrice
                if len(transaction.entries) != 1 or transaction.field_80:
                    raise ValueError("invalid quickbar extension transaction")
                entry = transaction.entries[0]
                with s.flow_lock:
                    current = s.gameplay_quickbar_info
                    next_count = int(current.quickbar_amount) + 1
                    # The client encodes this single direct purchase with
                    # amount=3; it is not a request for three extensions.
                    expected_offer = f"quickbar_{next_count - 1}"
                    if (entry.amount not in {1, 3} or next_count > 10
                            or entry.offer_id not in {"quickbar_1", expected_offer}):
                        raise ValueError(
                            f"quickbar extension is not the next unlock: "
                            f"offer={entry.offer_id!r}, amount={entry.amount}, "
                            f"owned={current.quickbar_amount}, expected={expected_offer}"
                        )
                    with sqlite3.connect(
                        self.static_database_path.resolve().as_uri() + "?mode=ro", uri=True
                    ) as database:
                        row = database.execute(
                            "SELECT PriceRC, Item, DirectlyBuyOffer FROM _Template_Offer WHERE Id=?",
                            (expected_offer,),
                        ).fetchone()
                    if (row is None or row[1] != entry.offer_id
                            or not row[2] or int(row[0]) <= 0):
                        raise ValueError("invalid direct quickbar offer")
                    if s.gameplay_currency_state is None or s.gameplay_character is None:
                        raise ValueError("player currency is not initialized")
                    price = PurchasePrice(rc=int(row[0]))
                    updated_currency, _, _ = debit_purchase_currency(
                        s.gameplay_currency_state, dict(s.gameplay_game_currencies), price,
                        shop_currency_resource=None,
                    )
                    updated_bar = replace(current, quickbar_amount=next_count)
                    packet, packet_bits = encode_server_command_batch((
                        ServerCommand(CURRENCY_CHANGED_COMMAND, updated_currency, LOCAL_PLAYER_ACTOR_ID),
                        ServerCommand(QUICKBAR_INFO_COMMAND, updated_bar, LOCAL_PLAYER_ACTOR_ID),
                        ServerCommand(UPDATE_PROGRESS_TIER_SPENDER_CHALLENGES_COMMAND,
                                      UpdateProgressTierSpenderChallenges(price.rc), 0),
                    ), include_terminator=True)
                    s.gameplay_currency_state = updated_currency
                    s.gameplay_quickbar_info = updated_bar
                CharacterService._send_world_loot_state_batch(
                    s, packet, packet_bits, label="quickbar-extension-purchase")
                CharacterService._request_account_save(s, "quickbar extension purchase", flush=True)
                return
            if transaction.shop_id != open_shop_id:
                raise ValueError(
                    "transaction does not match the currently opened shop"
                )
            if (
                inventory is None
                or currency is None
                or character is None
            ):
                raise ValueError("player inventory is not initialized")

            # ClientTradingManager writes operation 4 for sale and 5 for
            # restore in the final uint32, with an empty offer id in both.
            item_entries = tuple(e for e in transaction.entries if not e.offer_id)
            if item_entries:
                if len(item_entries) != len(transaction.entries):
                    raise ValueError("transaction mixes items and shop offers")
                if open_offers is None or not open_offers.player_can_sell:
                    raise ValueError("this shop does not allow item trading")
                builder = InventoryMutationBuilder(inventory, next_item_id)
                gold_delta = 0
                traded_templates = []
                for entry in item_entries:
                    if entry.amount == 4:
                        item = builder.item(entry.field_58)
                        if int(item.item_id) in builder.inventory.equipment_slots:
                            raise ValueError("equipped items cannot be sold")
                        if not (int(item.item_id) in builder.inventory.storage_58
                                or int(item.item_id) in builder.inventory.storage_80):
                            raise ValueError("sale item must be in the backpack")
                        value = item_sell_value(self.static_database_path, item, item.amount)
                        sold = builder.consume(item.item_id)
                        traded_templates.append(sold.template_id)
                        buyback.insert(0, (sold, value))
                        # Bound retained item data per character; newest first.
                        buyback = buyback[:20]
                        gold_delta += value
                    elif entry.amount == 5:
                        index = next((i for i, (item, _) in enumerate(buyback)
                                      if int(item.item_id) == int(entry.field_58)), None)
                        if index is None:
                            raise ValueError("item is no longer available for repurchase")
                        item, value = buyback.pop(index)
                        traded_templates.append(item.template_id)
                        definition = load_purchase_item_definition(
                            self.static_database_path, item.template_id)
                        builder.restore_item(item, ingredient_bag=definition.ingredient_bag)
                        gold_delta -= value
                    else:
                        raise ValueError("unknown item trading operation")
                if int(currency.gold) + gold_delta < 0:
                    raise ValueError("not enough gold to repurchase items")
                currency = replace(currency, amount=int(currency.gold) + gold_delta)
                mutation = builder.finish()
                refreshed = replace(open_offers, field_60=True, offers=(),
                    payment_items=(), categories=(),
                    sold_items=tuple(copy.deepcopy(item) for item, _ in buyback))
                packet, packet_bits = encode_server_command_batch((
                    ServerCommand(INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID),
                    ServerCommand(CURRENCY_CHANGED_COMMAND, currency, LOCAL_PLAYER_ACTOR_ID),
                    ServerCommand(INVENTORY_INFO_COMMAND, mutation.final, LOCAL_PLAYER_ACTOR_ID),
                    ServerCommand(OFFER_COMMAND, refreshed, 0),
                ), include_terminator=True)
                with s.flow_lock:
                    s.gameplay_currency_state = currency
                    s.gameplay_inventory = mutation.inventory
                    s.gameplay_next_item_id = mutation.next_item_id
                    s.gameplay_vendor_buyback = tuple(buyback)
                CharacterService._send_world_loot_state_batch(
                    s, packet, packet_bits, label="vendor-item-transaction")
                CharacterService._request_account_save(
                    s, "vendor sale/repurchase", flush=True)
                if _transaction_touches_npc_gates(traded_templates):
                    CharacterService._quest_refresh_npc_infos(self, s)
                return
            if open_offers is None:
                raise ValueError(
                    "the currently opened shop has no purchase offers"
                )
            if transaction.field_80:
                raise ValueError("unsupported transaction mode")

            offers_by_id = {
                offer.resource_id: offer
                for offer in open_offers.offers
                if offer is not None
            }
            purchases = []
            total_price = None
            for entry in transaction.entries:
                if not 1 <= entry.amount <= 1000:
                    raise ValueError("transaction amount is outside 1..1000")
                offer = offers_by_id.get(entry.offer_id)
                if offer is None:
                    raise ValueError(
                        f"offer {entry.offer_id!r} is not in shop "
                        f"{transaction.shop_id!r}"
                    )
                definition = load_purchase_item_definition(
                    self.static_database_path, offer.item_id
                )
                price = price_for_offer(offer, entry.amount)
                total_price = (
                    price if total_price is None else total_price + price
                )
                purchases.append((entry, offer, definition))

            shop_currency_resource = load_shop_currency_resource(
                self.static_database_path, transaction.shop_id
            )
            (
                currency,
                game_currencies,
                game_currency_update,
            ) = debit_purchase_currency(
                currency,
                game_currencies,
                total_price,
                shop_currency_resource=shop_currency_resource,
            )
            changed_items = []
            max_roll_item_ids = set()
            replaced_item_ids = []
            replacements = {}
            timed_offers = []
            now = datetime.now(timezone.utc)
            timestamp = (
                now.year, now.month, now.day,
                now.hour, now.minute, now.second,
            )
            timed_counts = dict(s.gameplay_timed_offer_purchases)
            for entry, offer, definition in purchases:
                mutation = add_purchased_item(
                    inventory,
                    definition,
                    offer.amount * entry.amount,
                    next_item_id=next_item_id,
                    item_level=(
                        offer.item_level_override
                        or max(1, int(character.level))
                    ),
                )
                if offer.unlocked_socket_count:
                    for purchased_item in mutation.changed_items:
                        purchased_item.socket_count = (
                            offer.unlocked_socket_count
                        )
                        purchased_item.socketed_templates = [
                            "" for _ in range(offer.unlocked_socket_count)
                        ]
                if offer.base_enchantment_tier_override >= 0:
                    for purchased_item in mutation.changed_items:
                        purchased_item.field_38 = (
                            offer.base_enchantment_tier_override
                        )
                next_item_id = mutation.next_item_id
                changed_items.extend(mutation.changed_items)
                if offer.enchantment_roll_quality_override >= 0:
                    max_roll_item_ids.update(
                        int(item.item_id) for item in mutation.changed_items
                    )
                replaced_item_ids.extend(mutation.replaced_item_ids)
                replacements.update(mutation.replacements)
                if offer.timed_offer:
                    count = timed_counts.get(offer.resource_id, 0) + entry.amount
                    timed_counts[offer.resource_id] = count
                    timed_offers.append(
                        TimedOffer(
                            offer.resource_id, count, 0, timestamp
                        )
                    )

            changed_item_ids = tuple(
                int(item.item_id) for item in changed_items
            )
            enchanted_item_ids = roll_new_equipment_enchantments(
                self.static_database_path,
                inventory,
                changed_item_ids,
                class_name=CLASS_NAMES[int(character.class_id)].lower(),
                map_difficulty=int(s.gameplay_map_difficulty),
            )
            if enchanted_item_ids:
                items_by_id = {
                    int(item.item_id): item for item in inventory.items
                }
                for item_id in max_roll_item_ids:
                    item = items_by_id.get(item_id)
                    if item is None:
                        continue
                    item.enchantments = [
                        replace(enchantment, value=1.0)
                        for enchantment in item.enchantments
                    ]
                changed_items = [
                    items_by_id[int(item.item_id)]
                    for item in changed_items
                ]
            refresh_inventory_economy_values(
                self.static_database_path,
                inventory,
                item_ids=tuple(
                    int(item.item_id) for item in changed_items
                ),
                force_melt_value=True,
            )
            items_by_id = {
                int(item.item_id): item for item in inventory.items
            }
            changed_items = [
                items_by_id[int(item.item_id)]
                for item in changed_items
            ]

            delta_inventory = clone_inventory_info(inventory, changed_items)
            delta_inventory.changed_item_ids = list(replaced_item_ids)
            delta_inventory.field_158 = dict(replacements)
            delta_inventory.field_1e0 = True
            final_inventory = clone_inventory_info(inventory, ())
            final_inventory.changed_item_ids = []
            final_inventory.field_158 = {}
            final_inventory.field_1e0 = False

            commands = []
            if total_price.rc:
                commands.append(
                    ServerCommand(
                        UPDATE_PROGRESS_TIER_SPENDER_CHALLENGES_COMMAND,
                        UpdateProgressTierSpenderChallenges(total_price.rc),
                        0,
                    )
                )
            if timed_offers:
                commands.append(
                    ServerCommand(
                        TIMED_OFFERS_INFO_COMMAND,
                        TimedOffersInfo(0, tuple(timed_offers)),
                        LOCAL_PLAYER_ACTOR_ID,
                    )
                )
            commands.extend((
                ServerCommand(
                    INVENTORY_INFO_COMMAND,
                    delta_inventory,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
                ServerCommand(
                    CURRENCY_CHANGED_COMMAND,
                    currency,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
            ))
            if game_currency_update is not None:
                commands.append(
                    ServerCommand(
                        GAME_CURRENCY_CHANGED_COMMAND,
                        game_currency_update,
                        LOCAL_PLAYER_ACTOR_ID,
                    )
                )
            commands.extend((
                ServerCommand(
                    INVENTORY_INFO_COMMAND,
                    final_inventory,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
                ServerCommand(
                    QUICKBAR_INFO_COMMAND,
                    s.gameplay_quickbar_info,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
                ServerCommand(
                    CURRENCY_CHANGED_COMMAND,
                    currency,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
            ))
            packet, packet_bits = encode_server_command_batch(
                tuple(commands), include_terminator=True
            )
        except ValueError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected Transaction: {exc}"
            )
            return

        with s.flow_lock:
            s.gameplay_currency_state = currency
            s.gameplay_inventory = inventory
            s.gameplay_game_currencies = game_currencies
            s.gameplay_next_item_id = next_item_id
            s.gameplay_timed_offer_purchases = timed_counts
        s.send_reliable_ordered_split(
            packet,
            total_bit_length=packet_bits,
            track=True,
            label_prefix='vendor-purchase-vendor-purchase',
        )
        if total_price.rc:
            self._credit_tier_spender_progress(s, total_price.rc)
            self._season_pass_feed(
                s, "SpendRealCurrency", count=total_price.rc,
            )
        if total_price.ic:
            if shop_currency_resource == "e_chr_2012_event_currency":
                self._season_pass_feed(
                    s, "SpendEventCurrency", count=total_price.ic,
                )
            else:
                self._season_pass_feed(
                    s, "SpendGameCurrency",
                    value=str(shop_currency_resource or ""),
                    count=total_price.ic,
                )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] handled Transaction for "
            f"{transaction.shop_id!r}: delivered "
            f"{[(item.template_id, item.amount) for item in changed_items]} "
            f"with price={total_price.as_tuple()} "
            f"({len(packet)} bytes/{packet_bits} bits)"
        )
        if _transaction_touches_npc_gates(
            item.template_id for item in changed_items
        ):
            CharacterService._quest_refresh_npc_infos(self, s)
        CharacterService._persist_gameplay_session(
            self, s, reason='vendor transaction'
        )
