"""Publish CMS schedule changes through native online event/status commands."""

from dataclasses import replace
from datetime import datetime, timezone

from dso_server.gameplay.event_system import event_status_effect_specs, event_status_entry
from dso_server.gameplay.season_pass import season_pass_event_id
from dso_server.protocol.gameplay_protocol import StatusEffect, encode_status_effect_batch
from dso_server.protocol.player_init_protocol import (
    EVENT_PROGRESS_INFO_COMMAND, EVENT_PARTICIPATION_INFO_COMMAND, EventUpdate,
    Notification, ServerCommand, encode_event_update, encode_server_command_batch,
)
from dso_server.protocol.reward_protocol import NOTIFICATION_COMMAND
from dso_server.services.admin_live import ready
from dso_server.services.events import EventService


def _season_run(catalog, template_id, now):
    return next((event.event_id for event in catalog.active_events(now)
                 if event.template.resource_id.casefold() == template_id), None)


def refresh_session_events(server, s, cid, context, previous, catalog):
    if not server.is_session_current(s):
        return False
    with s.flow_lock:
        if not ready(s, cid) or context != (s.gameplay_map, s.gameplay_chase_generation):
            return False
        level = s.gameplay_character.level
    specs = event_status_effect_specs(catalog, server.static_database_path, player_level=level)
    desired = {(spec.effect_id, spec.values): spec for spec in specs}
    with s.flow_lock:
        if not ready(s, cid) or context != (s.gameplay_map, s.gameplay_chase_generation):
            return False
        s.gameplay_event_catalog = catalog
        changes, keep = [], set()
        for instance_id in tuple(s.gameplay_event_status_effect_instance_ids):
            entry = s.gameplay_persistent_status_effects.get(instance_id)
            if entry is None:
                continue
            key = (entry.effect_id, tuple(entry.values))
            if key in desired:
                desired.pop(key)
                keep.add(instance_id)
            else:
                s.gameplay_persistent_status_effects.pop(instance_id)
                changes.append(replace(entry, has_details=False, values=()))
        for spec in desired.values():
            instance_id = s.gameplay_next_status_effect_instance_id
            s.gameplay_next_status_effect_instance_id += 1
            entry = event_status_entry(spec, instance_id=instance_id, actor_id=1)
            s.gameplay_persistent_status_effects[instance_id] = entry
            keep.add(instance_id)
            changes.append(entry)
        s.gameplay_event_status_effect_instance_ids = keep
    if changes:
        status = StatusEffect(clear_existing=False, entries=tuple(changes))
        s.send_reliable_ordered(encode_status_effect_batch(status, actor_id=1), track=True, label="admin-event-effects")
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, ("status_effects", (status,)))
    update, progress, participation = EventService._event_initialization_state(server, s)
    current_ids = {entry.event_id for entry in update.events}
    # EventUpdate also carries incremental activations. Explicitly send the
    # removed global instances as inactive; omission alone may leave client UI.
    now = datetime.now(timezone.utc)
    ended = (now.year, now.month, now.day, now.hour, now.minute, now.second)
    removed = tuple(replace(entry, flags=(False, *entry.flags[1:]), ends_at=ended)
                    for entry in previous.event_update().events if entry.event_id not in current_ids)
    packet, bits = encode_event_update(EventUpdate((*update.events, *removed)))
    s.send_reliable_ordered_split(packet, total_bit_length=bits, track=True, label_prefix="admin-events-update")
    commands = [ServerCommand(EVENT_PROGRESS_INFO_COMMAND, progress, 1),
                ServerCommand(EVENT_PARTICIPATION_INFO_COMMAND, participation, 1)]
    notifications = EventService._event_notification_entries_for_session(server, s, s.gameplay_character)
    if notifications:
        commands.append(ServerCommand(NOTIFICATION_COMMAND, Notification(1, notifications), 0))
    packet, bits = encode_server_command_batch(tuple(commands), include_terminator=True)
    s.send_reliable_ordered_split(packet, total_bit_length=bits, track=True, label_prefix="admin-events-state")
    # A season starting or ending moves progress to the new season; resend it
    # so the client's Season Pass window does not keep the previous one.
    season_template = season_pass_event_id(server.static_database_path).casefold()
    if (hasattr(server, "_season_pass_publish")
            and _season_run(previous, season_template, now) != _season_run(catalog, season_template, now)):
        server._season_pass_publish(s)
    return True
