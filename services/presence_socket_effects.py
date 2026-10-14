"""Map-presence delivery for group-targeted socket effect auras."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


class PresenceSocketEffectService:
    """Deliver proc auras only to nearby members of the owner's group."""

    def _handle_socket_ally_auras(
            self, session: Session, event, peers, character) -> bool:
        if not (
            isinstance(event, tuple)
            and event
            and event[0] == "socket_ally_auras"
        ):
            return False
        _kind, aura_events, source_position, server_tick = event
        source_group = self.social_store.group_for(
            int(character.character_id)
        )
        if source_group is None:
            return True
        for peer in peers:
            peer_character = peer.gameplay_character
            peer_move = peer.gameplay_last_move
            if (
                peer_character is None
                or peer_move is None
                or peer.gameplay_combat_state
                is not session.gameplay_combat_state
            ):
                continue
            peer_group = self.social_store.group_for(
                int(peer_character.character_id)
            )
            if (
                peer_group is None
                or int(peer_group.group_id) != int(source_group.group_id)
            ):
                continue
            distance = math.hypot(
                float(source_position[0])
                - float(peer_move.relative_position[0]),
                float(source_position[2])
                - float(peer_move.relative_position[2]),
            )
            definitions = tuple(
                definition
                for values, radius in aura_events
                if distance <= max(0.0, float(radius))
                for definition in values
            )
            if not definitions:
                continue
            CharacterService._send_player_skill_status_effects(
                peer,
                definitions,
                server_tick=int(server_tick) & 0xFFFFFFFF,
                label="socket-group-aura-status-effects",
            )
            if any(
                definition.modifier_kinds
                & {
                    "healthpoints", "healthpointsregeneration",
                    "maxhealthpoints", "maxskillresource",
                }
                for definition in definitions
            ):
                CharacterService._refresh_player_combat_vitals(peer)
        return True


__all__ = ["PresenceSocketEffectService"]
