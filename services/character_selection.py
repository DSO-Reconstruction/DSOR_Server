"""Character-selection flow and character creation."""

from __future__ import annotations
from dataclasses import replace
from dso_server.protocol.item_customization_protocol import item_tint
from dso_server.protocol.currency_protocol import STARTER_GOLD_UNITS

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.protocol.character_selection import encode_player_deleted


class CharacterSelectionService:
    """Character-selection flow and character creation."""

    def _schedule_post_selection_tail(self, s: Session):
        with s.flow_lock:
            if s.post_selection_config_sent or s.post_selection_tail_timer:
                return
            timer = threading.Timer(
                max(0, POST_SELECTION_CONFIG_DELAY_SECONDS),
                self._send_post_selection_tail_fallback,
                args=(s,),
            )
            timer.daemon = True
            s.post_selection_tail_timer = timer
        timer.start()
        print(
            "[DrasaCharacterService] waiting for client cmd 0x006f; "
            f"0x00dd starts in +{POST_SELECTION_CONFIG_DELAY_SECONDS:.3f}s"
        )

    def _send_post_selection_tail_fallback(self, s: Session):
        active = self.is_session_current(s)
        with s.flow_lock:
            s.post_selection_tail_timer = None
            already_sent = s.post_selection_config_sent
        if active and not already_sent:
            print(
                "[DrasaCharacterService] sending typed cmd 0x00dd "
                "on the official post-selection schedule"
            )
            self._send_post_selection_config(s)

    @staticmethod
    def _tail_fragments(command_id: int, packet: bytes):
        return tuple(
            packet[offset:offset + TAIL_FRAG_SIZE]
            for offset in range(0, len(packet), TAIL_FRAG_SIZE)
        )

    def _send_tail_items(self, s: Session, tail):
        index = 0
        while index < len(tail):
            command_id, packet, packet_bits = tail[index]

            trailing = ()
            if command_id == 0x010E and index + 1 < len(tail):
                next_command, next_packet, next_bits = tail[index + 1]
                if next_command == 0x010C:
                    trailing = ((next_packet, next_bits),)
                    index += 1

            if len(packet) > TAIL_FRAG_SIZE:
                fragments = self._tail_fragments(command_id, packet)
                pace = TAIL_PACE_SECONDS if len(fragments) > 32 else 0
                s.send_reliable_ordered_split(
                    fragments,
                    trailing_payloads=trailing,
                    pace_seconds=pace,
                    track=True,
                    label_prefix=(
                        f'serialized-0x{command_id:04x}-fragment'
                    ),
                    total_bit_length=packet_bits,
                )
            elif trailing:
                s.send_multi_reliable_ordered(
                    (packet, packet_bits), *trailing
                )
            else:
                s.send_reliable_ordered((packet, packet_bits))

            print(
                f"[DrasaCharacterService] sent "
                "serialized "
                f"cmd 0x{command_id:04x} "
                f"({len(packet)} bytes)"
            )
            index += 1

    def _send_post_selection_config(self, s: Session):
        """Send only cmd 0x00dd, matching the first half of session 57602.

        Client cmd 0x006f may arrive while this large response is in flight.
        The capture sends its final UI state as soon as 0x010b and 0x0087/op2
        arrive; the timeout preserves that outbound order for clients that do
        not emit those intermediate requests during a reconstructed session.
        """
        with s.flow_lock:
            if s.post_selection_config_sent:
                return
            s.post_selection_config_sent = True

        tail = [item for item in self.post_selection_tail if item[0] == 0x00DD]
        self._send_tail_items(s, tail)
        print(
            "[DrasaCharacterService] character-screen config complete; "
            "waiting for client cmds 0x010b and 0x0087/op2"
        )
        self._schedule_post_selection_completion(s)

    def _schedule_post_selection_completion(self, s: Session):
        with s.flow_lock:
            if s.post_selection_tail_sent or s.post_selection_completion_timer:
                return
            timer = threading.Timer(
                max(0, POST_SELECTION_TAIL_FALLBACK_SECONDS),
                self._send_post_selection_completion_fallback,
                args=(s,),
            )
            timer.daemon = True
            s.post_selection_completion_timer = timer
        timer.start()

    def _send_post_selection_completion_fallback(self, s: Session):
        active = self.is_session_current(s)
        with s.flow_lock:
            s.post_selection_completion_timer = None
            already_sent = s.post_selection_tail_sent
        if active and not already_sent:
            print(
                "[DrasaCharacterService] client readiness commands absent; "
                "sending serialized final UI tail"
            )
            self._send_post_selection_tail(s)

    def _send_post_selection_tail(self, s: Session, *, again: bool = False):
        """Send the second character list and final UI state after client readiness.

        ``again`` repeats it for a client that re-enters the selection screen
        (operation 2 after the creation editor's back button).
        """
        with s.flow_lock:
            if s.post_selection_tail_sent and not again:
                return
            s.post_selection_tail_sent = True
            timer = s.post_selection_completion_timer
            s.post_selection_completion_timer = None
        if timer is not None:
            timer.cancel()

        # The second character list is scoped like the first: this account's
        # characters, not the whole configured roster. An account with none
        # is left out of it entirely, for the same reason the first list is
        # withheld -- an empty list sends the client into its placeholder
        # character state.
        empty_account = self._visible_character_ids(s) == ()
        if empty_account:
            self._open_character_generation_editor(
                s, reason="account has no characters"
            )
            tail = [
                item
                for item in self.post_selection_tail
                if item[0] in (0x010E, 0x010C)
            ]
        else:
            scoped_payload, scoped_bits = self._account_selection_payload(s)
            tail = [
                (item[0], scoped_payload, scoped_bits)
                if item[0] == 0x0087
                else item
                for item in self.post_selection_tail
                if item[0] in (0x0087, 0x010E, 0x010C)
            ]
        self._send_tail_items(s, tail)

        print("[DrasaCharacterService] post-selection map tail complete")

    def _send_character_operation_response(
            self, s: Session, operation: int, character_id: int,
            equipment=()) -> bool:
        character = next(
            (
                entry for entry in self.character_selection.characters
                if entry.character_id == character_id
            ),
            None,
        )
        if character is None:
            print(
                "[DrasaCharacterService] rejected character operation "
                f"{operation} for "
                f"unknown character id {character_id}"
            )
            return False
        # Filtering the list only hides other accounts' characters; a tampered
        # client can still name one, and that ends the connection.
        if not self._require_character_ownership(
            s, character_id, action=f"character operation {operation}"
        ):
            return False

        response = copy.deepcopy(self.character_selection)
        response.operation = operation
        response.deny_reason = 0
        response.active_character_id = character_id
        response.characters = []
        response.equipment = copy.deepcopy(list(equipment))
        packet, bit_length = encode_character_selection(response)
        if len(packet) > TAIL_FRAG_SIZE:
            s.send_reliable_ordered_split(
                tuple(
                    packet[offset:offset + TAIL_FRAG_SIZE]
                    for offset in range(0, len(packet), TAIL_FRAG_SIZE)
                ),
                track=True,
                label_prefix=f'character-operation-{operation}-split',
                total_bit_length=bit_length,
            )
        else:
            s.send_reliable_ordered(
                (packet, bit_length),
                track=True,
                label=f'character-operation-{operation}',
            )
        print(
            f"[DrasaCharacterService] sent character operation {operation} "
            f"for {character.name} ({character_id}, "
            f"{len(response.equipment)} records)"
        )
        return True

    @staticmethod
    def _equipment_record_for(
            item, static_database=None, *, enabled: bool = True) -> EquipmentRecord:
        """One selection-screen record for an equipped inventory item.

        ``field_f8`` is the transmog skin and ``field_20`` is packed ARGB.
        Character-selection previews construct models from item templates, so
        newer skin ids are resolved to their authored ``TooltipItem`` model.
        The captured retail records also carry an ``effect_id``
        for set/weapon FX; its runtime source is not recovered, so it is left
        empty exactly as the character-creation records leave it.
        """

        tint = item_tint(item) or (0.0, 0.0, 0.0, 0.0)
        appearance_id = str(item.field_f8 or item.field_80 or "")
        item_id = str(item.template_id)
        if appearance_id and static_database is not None:
            try:
                visual_item = (
                    static_database.character_selection_visual_item_template(
                        item.template_id, appearance_id
                    )
                )
                if visual_item != item_id:
                    item_id = visual_item
                    # The resolved template already carries the selected
                    # model. Keep a same-id appearance override: basic
                    # equipment skins often use their own item template as
                    # the preview model while that template still points at
                    # the legacy base skin.
                    if visual_item != appearance_id:
                        appearance_id = ""
            except (OSError, ValueError, CharacterSelectionError):
                # Older static databases may not contain the preview link.
                pass
        return EquipmentRecord(
            item_id=item_id,
            appearance_id=appearance_id,
            tint=tint,
            enabled=bool(enabled),
        )

    def _persisted_selection_equipment(
            self, character_id: int) -> list[EquipmentRecord] | None:
        """What the character is actually wearing, or ``None`` if unknown.

        The selection screen renders one record per occupied equipment slot,
        so a two-handed weapon appears in both hand slots exactly as it does
        in the captured retail listing.
        """

        store = getattr(self, "account_store", None)
        if store is None:
            return None
        state = store.get(int(character_id))
        inventory = getattr(state, "inventory", None)
        if inventory is None:
            return None
        hidden_skins = getattr(state, "hidden_equipment_skins", {})
        items = {int(item.item_id): item for item in inventory.items}
        by_slot = {}
        for item_id, slots in inventory.equipment_slots.items():
            item = items.get(int(item_id))
            if item is None:
                continue
            for slot in slots:
                by_slot[int(slot)] = item
        records = []
        for slot in sorted(by_slot):
            item = by_slot[slot]
            customization_slot = 12 if slot == 7 else slot
            hidden_skin = str(hidden_skins.get(customization_slot, ""))
            item_skin = str(item.field_f8 or item.field_80 or "")
            enabled = not hidden_skin or bool(
                item_skin and item_skin != hidden_skin
            )
            records.append(
                CharacterSelectionService._equipment_record_for(
                    item,
                    getattr(self, "static_database", None),
                    enabled=enabled,
                )
            )
        return records

    def _selection_equipment(self, character_id: int) -> list[EquipmentRecord]:
        """Equipment for one character's selection-screen model.

        Persisted gear wins over ``character_equipment``, which holds the
        class's character-creation loadout and is only correct for a character
        that has never played.
        """

        equipped = CharacterSelectionService._persisted_selection_equipment(
            self, character_id
        )
        if equipped is not None:
            return equipped
        return list(self.character_equipment.get(int(character_id), []))

    def _send_character_equipment(
            self, s: Session, character_id: int) -> bool:
        return self._send_character_operation_response(
            s,
            9,
            character_id,
            CharacterSelectionService._selection_equipment(
                self, character_id
            ),
        )

    def _handle_character_delete(self, s: Session, character_id: int) -> None:
        """Client operation 6 of 0x0087: delete one of the account's characters.

        The character leaves the roster, the account's claim and the player
        database.  The client gets PlayerDeletedCommand for the id, then the
        account's remaining list -- or the creation editor when nothing is
        left, since an empty list would send it into its placeholder
        character (see ``_send_character_selection``).
        """

        character_id = int(character_id)
        if not self._require_character_ownership(
            s, character_id, action="deletion", drop=False
        ):
            return
        with self.character_roster_lock:
            characters = self.character_selection.characters
            index = next(
                (
                    position for position, entry in enumerate(characters)
                    if int(entry.character_id) == character_id
                ),
                None,
            )
            if index is None:
                print(
                    "[DrasaCharacterService] refused deletion of unknown "
                    f"character {character_id}"
                )
                return
            character = characters.pop(index)
            self.character_equipment.pop(character_id, None)
            if int(self.character_selection.active_character_id) == character_id:
                self.character_selection.active_character_id = (
                    int(characters[0].character_id) if characters else 0
                )
            active = int(self.character_selection.active_character_id)
            self.character_roster_revision += 1
        self._release_character_from_account(s, character_id)
        if self.account_store is not None:
            self.account_store.delete(character_id, flush=True)
        equipment = (
            copy.deepcopy(
                CharacterSelectionService._selection_equipment(self, active)
            )
            if active else []
        )
        with self.character_roster_lock:
            self.character_selection.equipment = equipment
        self._reencode_character_selection()
        with s.flow_lock:
            if s.selected_character_id == character_id:
                s.selected_character_id = None
        packet, bit_length = encode_player_deleted(character_id)
        s.send_reliable_ordered(
            (packet, bit_length), track=True, label="player-deleted"
        )
        print(
            f"[DrasaCharacterService] deleted character {character_id} "
            f"({character.name!r}) for account {s.account_id} "
            f"({s.account_username!r})"
        )
        visible = self._visible_character_ids(s)
        with self.character_roster_lock:
            roster_empty = not self.character_selection.characters
        if visible == () or (visible is None and roster_empty):
            self._open_character_generation_editor(
                s, reason="last character deleted", force=True
            )
        else:
            self._send_character_selection(s, force=True)

    @staticmethod
    def _send_character_generation_packet(
            s: Session, value: CharacterGeneration, *, label: str) -> None:
        packet, bit_length = encode_character_generation(
            value, server_to_client=True
        )
        s.send_reliable_ordered(
            (packet, bit_length),
            track=True,
            label=label,
        )
        print(
            "[DrasaCharacterService] sent CharacterGeneration "
            f"operation {value.operation} ({bit_length} bits; {label})"
        )

    def _send_character_generation_ocp(self, s: Session) -> None:
        with s.flow_lock:
            if (
                not s.character_generation_active
                or s.character_generation_ocp_sent
            ):
                return
            s.character_generation_ocp_sent = True
        self._send_tail_items(
            s,
            (
                (0x010E, EMPTY_OCP_OFFERS, EMPTY_OCP_OFFERS_BITS),
                (0x010C, EMPTY_OCP_METHODS, EMPTY_OCP_METHODS_BITS),
            ),
        )
        print(
            "[DrasaCharacterService] sent typed character-generation "
            "offer/method refresh"
        )

    def _next_character_id(self) -> int:
        used = {
            int(character.character_id)
            for character in self.character_selection.characters
        }
        candidate = max(
            max(used, default=LOCAL_CHARACTER_ID_BASE - 1),
            LOCAL_CHARACTER_ID_BASE - 1,
        ) + 1
        if candidate > 0xFFFFFFFF:
            candidate = LOCAL_CHARACTER_ID_BASE
            while candidate in used:
                candidate += 1
        return candidate

    # Denial codes the client renders, in the order its strings are declared
    # in gui.window.xml. Only DENY_NAME_EXISTS is confirmed against the live
    # client; the rest follow that ordering, and an unmapped cause falls back
    # to the generic message rather than a misleading one.
    DENY_NAME_EXISTS = 1        # "The selected name already exists!"
    DENY_NAME_EMPTY = 2         # "Please enter a name with at least N letters."
    DENY_NAME_INVALID = 3       # "The name includes an unsupported character."
    DENY_ALREADY_GENERATED = 7  # "Character was already generated!"
    DENY_UNKNOWN = 8            # "Unknown error."

    def _character_generation_denial(
            self, s: Session, request: CharacterGeneration, reason: str,
            *, deny_reason: int = DENY_UNKNOWN,
    ) -> None:
        response = character_generation_result(
            request,
            operation=4,
            deny_reason=deny_reason,
        )
        self._send_character_generation_packet(
            s, response, label="character-generation-denied"
        )
        print(
            "[DrasaCharacterService] rejected character creation: "
            f"{reason} (deny reason {deny_reason})"
        )

    def _open_character_generation_editor(
            self, s: Session, *, reason: str, force: bool = False,
            character=None) -> bool:
        """Send operation 2, which opens and initializes the client editor.

        The client asks for this when the player clicks "create character".
        An account with no characters never gets that far -- there is no
        selection screen to click on -- so the server opens the editor itself,
        which is what leaves a fresh account somewhere it can act instead of
        staring at an empty roster.

        ``force`` is for that client request, which must always be answered.
        The editor the server opens on its own is only the entry point: the
        client still asks for the editor data itself, and without a reply to
        *that* request its class buttons and create button do nothing.
        """

        with s.flow_lock:
            if s.character_generation_active and not force:
                return False
            s.character_generation_active = True
            s.character_generation_ocp_sent = False
            s.character_generation_editing_id = int(
                getattr(character, "character_id", 0)
            )
        if character is not None:
            preview_id = int(character.character_id)
        else:
            with self.character_roster_lock:
                preview_id = self._next_character_id()
        response = character_generation_editor_response(
            locale=self.character_selection.locale,
            preview_id=preview_id,
            character=character,
            operation=9 if character is not None else 2,
        )
        self._send_character_generation_packet(
            s, response, label="character-generation-editor"
        )
        # Existing characters enter the editor after the selection screen's
        # readiness handshake has already completed. Refresh the editor's
        # OCP offer/method tail here as well; otherwise the client can leave
        # the selection view without receiving the editor-ready tail.
        self._send_character_generation_ocp(s)
        print(
            "[DrasaCharacterService] opened typed "
            f"{'character-alteration' if character is not None else 'character-generation'} "
            f"editor (id {preview_id}; {reason})"
        )
        return True

    def _handle_character_alteration(
            self, s: Session, request: CharacterGeneration,
            character_id: int) -> None:
        """Commit a paid name/appearance/gender edit from character select."""
        registry = getattr(self, "account_registry", None)
        account_id = int(getattr(s, "account_id", 0) or 0)
        if registry is None or account_id <= 0:
            self._character_generation_denial(
                s, request, "character changes require an authenticated account"
            )
            return
        if not self._require_character_ownership(
            s, character_id, action="character alteration", drop=False
        ):
            return

        encoded_name = request.name.encode("utf-8", errors="strict")
        if not request.name:
            self._character_generation_denial(
                s, request, "empty character name",
                deny_reason=self.DENY_NAME_EMPTY,
            )
            return
        if (
            request.name != request.name.strip()
            or len(encoded_name) > 32
            or any(ord(character) < 0x20 for character in request.name)
        ):
            self._character_generation_denial(
                s, request, "invalid character name",
                deny_reason=self.DENY_NAME_INVALID,
            )
            return
        if not 0 <= request.gender <= 1:
            self._character_generation_denial(
                s, request, f"unknown gender id {request.gender}"
            )
            return

        denial_reason = None
        denial_code = self.DENY_UNKNOWN
        updated = None
        with self.character_roster_lock:
            current = next(
                (value for value in self.character_selection.characters
                 if int(value.character_id) == int(character_id)),
                None,
            )
            if current is None:
                denial_reason = "character no longer exists"
            elif int(request.class_id) != int(current.class_id):
                denial_reason = "character alteration cannot change class"
            elif any(
                value.character_id != int(character_id)
                and value.name.casefold() == request.name.casefold()
                for value in self.character_selection.characters
            ):
                denial_reason = "character name already exists"
                denial_code = self.DENY_NAME_EXISTS
            elif registry.character_alteration_bookings(account_id) <= 0:
                denial_reason = "account has no character alteration bookings"
                denial_code = self.DENY_ALREADY_GENERATED
            else:
                profile = (
                    self.account_store.get_profile(character_id)
                    if self.account_store is not None else None
                )
                saved_profile = replace(
                    profile or CharacterProfile.from_character_record(current),
                    name=request.name,
                    gender=request.gender,
                    hair_style=request.hair_style,
                    beard_style=request.beard_style,
                    body_style=request.body_style,
                    body_stature=request.body_stature,
                )
                if not registry.consume_character_alteration_booking(
                    account_id
                ):
                    denial_reason = "character alteration booking was already used"
                    denial_code = self.DENY_ALREADY_GENERATED
                else:
                    try:
                        if self.account_store is not None:
                            self.account_store.put_profile(
                                saved_profile, flush=True
                            )
                        current.name = request.name
                        current.gender = request.gender
                        current.hair_style = request.hair_style
                        current.beard_style = request.beard_style
                        current.body_style = request.body_style
                        current.body_stature = request.body_stature
                        self.character_roster_revision += 1
                        updated = copy.deepcopy(current)
                    except Exception:
                        registry.grant_character_alteration_booking(
                            account_id
                        )
                        raise

        if denial_reason is not None:
            self._character_generation_denial(
                s, request, denial_reason, deny_reason=denial_code
            )
            return

        with s.flow_lock:
            s.character_generation_active = False
            s.character_generation_editing_id = 0
        self._reencode_character_selection()
        self._send_character_generation_packet(
            s,
            character_generation_result(
                request, operation=11, character_id=int(character_id)
            ),
            label="character-alteration-saved",
        )
        self._send_character_selection(s, force=True)
        print(
            "[DrasaCharacterService] applied character alteration for "
            f"{updated.name!r} ({character_id}); "
            f"{registry.character_alteration_bookings(account_id)} booking(s) remain"
        )

    def _handle_character_generation(
            self, s: Session, payload: bytes) -> None:
        try:
            request = decode_character_generation(payload)
        except (ValueError, CharacterSelectionError) as exc:
            print(
                "[DrasaCharacterService] rejected malformed "
                f"CharacterGenerationCommand: {exc}"
            )
            return

        if request.operation in (1, 8):
            requested_character_id = int(request.character_id)
            if requested_character_id:
                if not self._require_character_ownership(
                    s, requested_character_id,
                    action="character alteration", drop=False,
                ):
                    return
                registry = getattr(self, "account_registry", None)
                account_id = int(getattr(s, "account_id", 0) or 0)
                if (
                    registry is None or account_id <= 0
                    or registry.character_alteration_bookings(account_id) <= 0
                ):
                    self._character_generation_denial(
                        s, request,
                        "account has no character alteration bookings",
                        deny_reason=self.DENY_ALREADY_GENERATED,
                    )
                    return
                with self.character_roster_lock:
                    target = next(
                        (value for value in self.character_selection.characters
                         if int(value.character_id) == requested_character_id),
                        None,
                    )
                    target = copy.deepcopy(target)
                if target is None:
                    return
                self._open_character_generation_editor(
                    s,
                    reason=(
                        "paid character alteration button"
                        if request.operation == 8
                        else "paid character alteration"
                    ),
                    force=True,
                    character=target,
                )
                return
            if request.operation == 8:
                self._character_generation_denial(
                    s, request,
                    "character alteration request did not name a character",
                )
                return
            self._open_character_generation_editor(
                s, reason="client request", force=True
            )
            return

        if request.operation == 10:
            with s.flow_lock:
                editing_id = int(s.character_generation_editing_id or 0)
            requested_id = int(request.character_id)
            existing_id = requested_id or editing_id
            if existing_id and any(
                int(character.character_id) == existing_id
                for character in self.character_selection.characters
            ):
                if editing_id and requested_id and editing_id != requested_id:
                    self._character_generation_denial(
                        s, request, "character alteration target changed"
                    )
                    return
                self._handle_character_alteration(
                    s, request, existing_id
                )
            else:
                self._character_generation_denial(
                    s, request,
                    "character alteration save did not name an existing character",
                )
            return

        if request.operation != 3:
            print(
                "[DrasaCharacterService] ignored unsupported "
                f"CharacterGeneration operation {request.operation}"
            )
            return

        with s.flow_lock:
            editing_id = int(s.character_generation_editing_id or 0)
        requested_id = int(request.character_id)
        existing_id = requested_id or editing_id
        if existing_id and any(
            int(character.character_id) == existing_id
            for character in self.character_selection.characters
        ):
            if editing_id and requested_id and editing_id != requested_id:
                self._character_generation_denial(
                    s, request, "character alteration target changed"
                )
                return
            self._handle_character_alteration(
                s, request, existing_id
            )
            return

        encoded_name = request.name.encode("utf-8", errors="strict")
        if not request.name:
            self._character_generation_denial(
                s, request, "empty character name",
                deny_reason=self.DENY_NAME_EMPTY,
            )
            return
        if (
            request.name != request.name.strip()
            or len(encoded_name) > 32
            or any(ord(character) < 0x20 for character in request.name)
        ):
            self._character_generation_denial(
                s, request, "invalid character name",
                deny_reason=self.DENY_NAME_INVALID,
            )
            return
        if request.class_id not in CLASS_NAMES:
            self._character_generation_denial(
                s, request, f"unknown class id {request.class_id}"
            )
            return
        if not 0 <= request.gender <= 1:
            self._character_generation_denial(
                s, request, f"unknown gender id {request.gender}"
            )
            return
        if not map_database_path(
            self.static_database_path, NEW_CHARACTER_GAMEPLAY_MAP
        ).is_file():
            self._character_generation_denial(
                s,
                request,
                f"new-character map {NEW_CHARACTER_GAMEPLAY_MAP!r} is absent",
            )
            return

        # Static content access can touch SQLite and must not extend the
        # roster transaction. Duplicate-name and slot checks are repeated in
        # the short commit below, which preserves their atomicity.
        try:
            template_row = (
                self.static_database.player_template_row_index(
                    request.class_id
                )
                if self.static_database is not None
                else request.class_id
            )
            equipment = (
                self.static_database.character_generation_equipment(
                    request.class_id
                )
                if self.static_database is not None
                else []
            )
        except (OSError, ValueError) as exc:
            self._character_generation_denial(
                s, request, f"class assets are unavailable: {exc}"
            )
            return

        denial_reason = None
        denial_code = self.DENY_UNKNOWN
        # Slots belong to the account, not to the server. Counting the whole
        # roster made every new account full as soon as the configured
        # characters existed, which is what denied creation to fresh accounts.
        owned = self._owned_character_ids(s)
        with self.character_roster_lock:
            occupied = (
                len(owned)
                if owned is not None
                else len(self.character_selection.characters)
            )
            if occupied >= self.character_selection.character_slots:
                # The client's editor has no string for this: retail never
                # offers creation once the slots are full.
                denial_reason = "all character slots are occupied"
            elif any(
                character.name.casefold() == request.name.casefold()
                for character in self.character_selection.characters
            ):
                # Names stay unique across the whole server, not per account.
                denial_reason = "character name already exists"
                denial_code = self.DENY_NAME_EXISTS
            else:
                character_id = self._next_character_id()
                character = CharacterRecord(
                    name=request.name,
                    map_id=NEW_CHARACTER_GAMEPLAY_MAP,
                    class_id=request.class_id,
                    gender=request.gender,
                    hair_style=request.hair_style,
                    beard_style=request.beard_style,
                    body_style=request.body_style,
                    body_stature=request.body_stature,
                    character_id=character_id,
                    unknown_u32_a=0,
                    premium_currency=100_000_000,
                    progression_u64=0,
                    progression_u32=0,
                    level=1,
                )
                self.character_selection.characters.append(character)
                self.character_selection.active_character_id = character_id
                self.character_selection.equipment = copy.deepcopy(equipment)
                self.character_equipment[character_id] = copy.deepcopy(
                    equipment
                )
                self.player_template_rows[request.class_id] = template_row
                self.character_roster_revision += 1

        if denial_reason is not None:
            self._character_generation_denial(
                s, request, denial_reason, deny_reason=denial_code
            )
            return

        self._claim_character_for_account(s, character.character_id)
        self._reencode_character_selection()
        if self.account_store is not None:
            self.account_store.put_profile(
                CharacterProfile.from_character_record(
                    character, gold=STARTER_GOLD_UNITS
                ),
                flush=False,
            )

        self.arm_gameplay(
            s.addr[0],
            NEW_CHARACTER_GAMEPLAY_MAP,
            character,
            template_row,
        )
        if self.gameplay_router is not None:
            arm_login_handoff = getattr(
                self.gameplay_router,
                "arm_gameplay_handoff",
                self.gameplay_router.arm_gameplay,
            )
            arm_login_handoff(
                s.addr[0],
                NEW_CHARACTER_GAMEPLAY_MAP,
                character,
                template_row,
            )
        with s.flow_lock:
            s.selected_character_id = character_id
            s.character_generation_active = False

        response = character_generation_result(
            request,
            operation=5,
            character_id=character_id,
        )
        self._send_character_generation_packet(
            s, response, label="character-generation-created"
        )

        # Creation ends in the world, exactly like picking a character from
        # the list, and it needs the same redirect. SwitchMapCommand is what
        # sets the client's game-map flag; without it the client still
        # reconnects to the gameplay service, but never constructs its
        # IngameWidget, and the first command that reaches for the inventory
        # widget trips "this->inGameWidget.isvalid()". The final flag must
        # stay false, or the client clears that flag and returns to character
        # selection after connecting.
        gameplay_addr = (
            f'{self.advertised_host}:{getattr(self.gameplay_router, "port", 2190)}'
            if self.gameplay_router is not None
            else f'{self.advertised_host}:{self.port}'
        )
        s.send_reliable_ordered(
            switch_map_payload(
                gameplay_addr, enter_character_selection=False
            ),
            track=True,
            label='switch-map-new-character',
        )
        print(
            "[DrasaCharacterService] created character "
            f"{character.name!r} ({character_id}, "
            f"{CLASS_NAMES[character.class_id]}) and armed "
            f"{NEW_CHARACTER_GAMEPLAY_MAP}; sent SwitchMapCommand to "
            f"{gameplay_addr}"
        )
