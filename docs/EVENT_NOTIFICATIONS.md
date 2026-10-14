# Event notifications on the first map join

Design for announcing the active `events.json` entries with a
`NotificationCommand` (`0x0088`) popup once per login session. The proposed
implementation below is now in place (see *Implementation status*); what
still has to be measured against the running client is unchanged.

## Implementation status

Implemented on 2026-09-09, following the plan in *Proposed implementation*:

| Piece | Where |
| --- | --- |
| login session id kept on the socket | `Session.login_session_id`, set in `RakNetServer._identify_account()` |
| entries, GUID, session key, memory | `services/events.py`: `event_notification_entries()`, `event_notification_guid()`, `event_notification_session_key()`, `EventNotificationMemory` |
| kind | `EVENT_NOTIFICATION_KIND` in `services/events.py`: 7 (`eventnonpersistent`, database row order), overridable with `DSO_EVENT_NOTIFICATION_KIND` |
| hook | `_build_initialization_commands()` appends `_event_notification_entries_for_session()` after the daily login entry |
| reply | `_handle_notification_action()` acknowledges any reply whose payload carries `eventId` or whose kind is the event kind, and logs the subtype |
| tests | `tests/test_event_notifications.py` |

The memory lives on whichever service instance initializes the player (the
character service or the legacy 2190 route), keyed `session:<login GUID>`
or, without a registry, `character:<id>`. Entries expire after the
registry's session TTL and the map is capped at 4096 keys. The
initialization log prints `announcing N active event(s) to <addr>` when
entries are added, and the reply handler prints the subtype the client
answered with, which is the capture the *Reply* bullet below asks for.

## What exists

**Choosing the events.** `EventCatalog.active_events(now)` in
`gameplay/event_system.py` returns the `ScheduledEvent` entries whose state
is `active` at `now`, each carrying its `event_id` and the
`EventTemplateDefinition` loaded from `_Template_Events`. `CharacterService`
reaches the catalog through `EventService._event_catalog()` in
`services/events.py`, which also caches it on the session as
`s.gameplay_event_catalog` during event initialization. With the shipped
schedule only `scaling_dungeon_kill_event_01` (event 35239) is active;
`desert_of_essences_02` (35238) expired on 2026-09-09.

**Sending them.** `CharacterService._build_initialization_commands()` in
`services/player_init_world.py` assembles a tuple of `NotificationEntry`
values: pending OCP bookings (kind 16) and, when a reward is due, the daily
login popup (kind 6). The tuple becomes one `NotificationCommand` with
notification type `1` inside the initialization `0x85` batch, built next to
`empty_notification` in `protocol/player_init_protocol.py`. Entries are made
with `make_notification_entry(kind, instance_guid, values, payload,
timestamp)`; the wire encoder `_write_notification_body` writes each entry
as kind, 16-byte GUID blob, three `uint32`, the JSON payload string, and a
timestamp string. Event entries are appended to that same tuple.

**Once per login session.** Every hello the client sends carries its login
session GUID; `RakNetServer._identify_account()` in `runtime/transport.py`
resolves it with `AccountRegistry.find_session()` to a `SessionRecord` and
stores `account_id`, `account_username`, and `account_character_ids` on the
socket. The record also has `session_id`, which is not kept today. A login
session survives map transfers (each transfer is a new UDP session that
presents the same GUID) and is rotated on relog, so "first map join of the
session" is "first initialization seen for this `session_id`".

**The reply.** A click or dismiss comes back as a client
`NotificationCommand` (type `2`) and is routed to
`_handle_notification_action()` in `services/commerce.py`, both from the
character service dispatch and from the legacy 2190 route in
`services/login_game.py`. The handler recognizes OCP bookings by kind,
invitations by an `invitation` payload key, daily login by
`dailyLoginCount`, and raises `unsupported notification action` for
anything else. Retail semantics seen there: subtype `1` is the confirming
button, subtype `2` the decline.

## Live result, 2026-09-09

The first build used kind `6`. On the live client every event entry rendered
as a copy of the daily-reward popup, not as an event popup. That settles the
kind table: `6` is `loginreward`, which is row 6 of `_Template_Notifications`
in the shipped database, so **the database row order is this build's enum**
and the string-pool order below is not. Under row order the event kinds are
`7` (`eventnonpersistent`) and `8` (`eventpersistent`); `20` is
`onetimeevent`. The server now sends `7`. If that renders nothing, set
`DSO_EVENT_NOTIFICATION_KIND=8` and restart; no code change is needed to try
a kind. The OCP booking kind `16` is row 16, `dailyachievementsfinished`,
which still does not fit, so that capture remains unexplained.

The payload (`eventId`, `templateId`) was not exercised by the kind-6 run: the
daily-reward handler ignores keys it does not know, so the duplicate popups
say nothing about whether the event handler will accept them.

## What the client binary says

Read from `dro_client64.exe` (release261_84) strings; nothing below was
captured on the wire.

**Notification kinds.** The client carries an ordered name table ending in
the symbol `NotificationToType::Type`. Counting from zero:

| Kind | Name | Kind | Name |
| --- | --- | --- | --- |
| 0 | test | 15 | dailyachievements |
| 1 | groupinvite | 16 | strike |
| 2 | groupjoinrequest | 17 | cloaksunlocked |
| 3 | guildinvite | 18 | onetimeevent |
| 4 | specialoffer | 19 | titleunlocked |
| 5 | expiringspecialoffer | 20 | membershipreminder |
| 6 | eventnonpersistent | 21 | membershippromo |
| 7 | sale | 22 | membershipcomparison |
| 8 | highlightsale | 23 | temporaryquickbarexpiration |
| 9 | talentup | 24 | tierspenderreward |
| 10 | newskill | 25 | dailytierspenderreward |
| 11 | maprework | 26 | steaminventoryreceive |
| 12 | bookingconfirmation | 27 | limittimetehuilibao |
| 13 | ingamebookingconfirmation | 28 | userbenefit |
| 14 | negativebooking | 29 | presalediscount |

This table is not `_Template_Notifications` (that one has `loginreward`,
`eventpersistent`, `tutorialvideohint`, which the client no longer names,
and lacks the newer entries), so the database row order must not be used
to derive kinds. The kinds the server already uses line up with it where a
capture exists: group invite `1`, guild invite `3`, and the daily login
popup `6`, which under this table is `eventnonpersistent`. That makes `6`
the natural kind for an event announcement: it is the generic
non-persistent event popup, and the JSON payload decides what it shows.
The one value that does not fit is the OCP booking kind `16` (`strike`
here, where `bookingconfirmation` is `12`); either that capture came from a
different build or the table order is not the enum order, so treat the
table as the best available guess, not a proof.

**Handlers and payload keys.** The client has a dedicated
`Game::ClientEventNotificationHandler` beside the social, booking, talent,
and popup handlers. In the string pool the keys `eventId` and `templateId`
sit directly against that handler's symbol, so it reads those two from the
payload dictionary. The daily-login keys (`reward`, `rewardIndex`,
`dailyLoginCount`, `premiumReward`) belong to
`Game::ClientPopUpNotificationHandler`. The event handler is the one that
will resolve `_Template_Events` itself: the row has `LocaleIdTitle`,
`LocaleIdDesc`, `NotificationIcon`, `IconBrush`, `AudioEvent`,
`TeleportTarget`, and `AdditionalEventElements`, so the server only has to
name the event.

**Client-side de-duplication.** `Game::PlayerNotificationInfo` exposes
`DiscardNotificationByGuid`, so the client keys pending notifications by
the entry GUID. A GUID derived from the event id is therefore also a guard
against showing one event twice inside one client run.

**The two shipped templates.**

| Template | Title / description locale ids | Icon | Teleport | Elements |
| --- | --- | --- | --- | --- |
| `scaling_dungeon_kill_event_01` | empty / `scaling_dungeon_kill_event_01_newProgress` | `ui_event/event_towndefense_03_crunch` | none | `ButtonShop;ButtonRealmoneyShop` |
| `desert_of_essences_02` | empty / empty | `ui_event/event_desertofessences_crunch` | `a0200_kingscity:a0200_fairground` | `ButtonTeleport;ButtonAchievements;ButtonShop;ButtonRealmoneyShop;EventShopLabel` |

Both have an empty `LocaleIdTitle`, so a popup without a title is a data
property of these rows, not a payload mistake.

## Proposed implementation

1. **Session id on the socket.** In `_identify_account()` add
   `s.login_session_id = record.session_id`, initialized to `None` next to
   `account_id` in `Session.__init__`.

2. **Per-session memory on the character server.** A dictionary
   `session key -> set of event ids already announced`, guarded by the
   server's existing lock, on `CharacterServer`. The key is
   `s.login_session_id` when set. Without a registry (loopback development
   never logs in) fall back to `f"character:{character_id}"`, which makes
   it once per server run per character rather than once per map, at the
   cost of not repeating after a relog. Prune entries older than the
   registry's session TTL, or whenever the set grows past a few thousand
   keys; the memory does not need to survive a restart, since a restart is
   rarer than a relog.

3. **Entries.** A pure helper in `services/events.py`:

   ```python
   def event_notification_entries(catalog, *, now, already_sent) -> tuple[NotificationEntry, ...]
   ```

   For each `catalog.active_events(now)` whose `event_id` is not in
   `already_sent`, build

   ```python
   make_notification_entry(
       EVENT_NOTIFICATION_KIND,            # 6 until measured otherwise
       uuid.uuid5(uuid.NAMESPACE_URL, f"dso-local-event-notification:{event.event_id}").bytes,
       (event.event_id, start_epoch, end_epoch),
       {"eventId": event.event_id, "templateId": event.template.resource_id},
   )
   ```

   The three values are not known to matter; `(event_id, start, end)` is a
   reasonable first guess, `(0, server_time(), server_time())` the fallback
   if the popup misbehaves. Leave the timestamp at its default, as the
   daily login does. Skip templates flagged `client_only`, which the
   client already surfaces on its own.

4. **Hook.** In `_build_initialization_commands()`, after the daily login
   entry is appended, extend `notifications` with the helper's result and
   record the event ids under the session key. Recording happens when the
   batch is built rather than when it is acknowledged; a failed
   initialization retries through a new UDP session, and the client will
   drop a repeated GUID anyway.

5. **Reply.** In `_handle_notification_action()`, before the
   `dailyLoginCount` check, accept any action whose kind is the event kind
   or whose payload carries `eventId`: call
   `CharacterService._accept_gameplay_state_command(s, CLIENT_NOTIFICATION_COMMAND)`
   and return. Subtype `1` on a template with a `TeleportTarget` and
   `ButtonTeleport` is the only case with a side effect; the active
   template has neither, so log it and do nothing until a capture shows
   what the client expects (it may issue its own `MapTravelCommand`). Do
   not raise: an unknown reply must not desync the session state machine.

6. **Tests.** Cover the helper (only active events, `already_sent`
   filtering, deterministic GUID, payload keys), the dedupe across two
   initializations with the same session id and a fresh set for a new
   id, the loopback fallback key, and the reply branch accepting subtypes
   `1` and `2` without raising. `tests/test_group_popup.py` shows how
   existing notification paths are driven.

## What must be measured on the client

Run with the kill event active and watch `DSOClient.log` and the screen:

- **Kind.** `6` is settled as the daily-reward popup (see *Live result*).
  The server sends `7`; if nothing appears try `8`, then `20`
  (`onetimeevent`), each via `DSO_EVENT_NOTIFICATION_KIND` and a restart.
- **Payload.** With `eventId` and `templateId` set, the popup should show
  the template's `NotificationIcon` and description locale. If it is
  blank, add `eventName` set to the template id; if the client asserts,
  the keys are read by another handler and the kind is wrong, not the
  keys.
- **Reply.** Note the subtype and payload the client sends back on each
  button so the handler can be tightened from "accept anything" to
  matching what was sent.
- **Repeat behavior.** Transfer maps once and relog once: the popup must
  appear only after the relog.
