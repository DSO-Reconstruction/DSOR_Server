# Map access and customization

This document records the three features completed with the production
refactor and the protocol/database evidence used by them.

## Click-only exits

Map movement never activates an exit. `MoveCommand` (`0x005f`) only updates
position, visibility, auto-loot, and monster simulation.

An exit transfer begins only after the client sends `UnlockMapCommand`
(`0x0060`) for a concrete exit. The server:

1. verifies the request names the active map;
2. resolves the exit from the map database;
3. disambiguates duplicate IDs using authoritative player position;
4. resolves the named destination entry;
5. persists and retires source presence;
6. arms the 2190/2192 handoff;
7. sends the actorless gameplay switch.

Standing on an exit, approaching it, or crossing its trigger cannot transfer
the player.

In-world travel objects (travel stones and waypoints) use `TravelCommand`
(`0x0061`) but with a distinct generated body: an optional mode byte, the
travel-point resource string, the destination map string, and the entry index.
The decoder identifies the body by parsing rather than by inspecting the byte
after the command id: each form's counted-string lengths land in different
places, so only the correct form consumes the payload exactly. That matters
because the mode byte is not always `0`/`1` and some objects omit it, while a
misread makes the destination string's length start mid-name. The mode value is
kept in `MapTravelRequest.mode` and is never fed to monster scaling; a scaled
destination rides in the map id (for example `a0303_wildcave_08`). Recognized
travel-object requests go through the same armed `SwitchMapCommand` loading-bar
handoff. Merely standing near a travel stone does not move the player. A body
matching no known form is logged with its raw bytes and dropped, so it cannot
abort the rest of the datagram.

Event-preview **Start** uses the native three-string TravelCommand body:
`source`, `mapId`, `entryName`, then one boolean. The source is empty. The
reported `a0200_kingscity` / `a0200_fairground` packet contains a named entry,
not a numeric index. Native serializer `0x140945bb4` and decoder `0x14093ec38`
confirm strings at object offsets `0x20`, `0x60`, `0xa0` and the bool at `0xe0`.
An empty source and entry are wire-identical to the existing zero-difficulty,
zero-index representation, which remains supported along with waypoint layouts.

The server matches the map/entry pair against an active, level-eligible,
non-silent event's `_Template_Events.TeleportTarget` (`map:entry`). It resolves
the physical map and named `_Instance_Entry` transform before arming the normal
map handoff. Event travel can reach an undiscovered destination, while event-map
and quest access gates still apply. It does not consume an unrelated armed
travel item. Missing event targets use Kingscity's default spawn; authored
entries absent from the installed destination use that map's default spawn.
Unknown or ineligible event targets are rejected. The command carries no event
id, so events with the same destination share authorization for that pair.
`tests/test_event_travel.py` replays the reported packet at exact and rounded
bit lengths and exercises eligibility, fallback, handoff, and duplicate clicks.

Travel stones are also protected monster-spawn objects. Spawn generation
rejects candidates inside the greater of the configured exclusion radius
(12 units by default) and the object's authored picking range plus four
units. The rejection occurs before actor registration for fixed, density,
and fallback candidates, so no trapped return-home actors are created.

## Travel blocked behind a map response

The 2026-09-09 regression affected ordinary fast travel and administrative
transfers together. With Anniversary active, Kingscity's PropRequest reply
contained 15 props (1290 payload bytes / 1304 bytes including RakNet framing).
It was sent unsplit and repeatedly failed delivery. The client ACKed the later
SwitchMap datagram but could not execute it ahead of the missing ordered reply.
The source session had already begun departure, producing the apparent freeze.

PropRequest now uses the existing fragmentation-aware world-state sender,
matching mid-map prop updates. `tests/test_map_travel_delivery.py` covers the
real event configuration, small replies, both switch envelopes, reordering and
retransmission of a lost fragment. Live normal travel and `!map` both completed
their destination initialization after this fix; details are in `ADMIN_CMS.md`.

## Map discovery and quest access

Travel destinations start locked. Map entry records the physical map, access ID
and access group in `AccountState.unlocked_maps`; NewPlayer's travel bitset is
built from these saved discoveries. Difficulty variants share their travel point.
The former `DSO_UNLOCK_ALL_TRAVEL_MAPS` testing override has been removed.

`_Template_MapAccessGroup.UnlockingQuest` grants its access IDs/groups when the
quest is turned in. Initialization also recovers these grants from already closed
quests in older saves. Normal exits and travel enforce authored quest requirements;
fast travel additionally requires discovery. Hub-return talents and validated
travel items retain their destination-specific behavior.

Administrators can use `!unlockmaps [character name]` or **Unlock all maps** under
the CMS character's Live actions. `AccountState.admin_all_maps` persists this
per-character bypass without manufacturing quest completions or visited tutorial
maps. Event availability still applies. The client receives the updated travel
bitset on its next map load or relog.

A character-screen `LogoutCommand` (`0x0063`, including `8b630040`) only
acknowledges disconnection. It must not arm or replace a gameplay route: doing
so can make a quick relog skip the Play transition, leaving the client without
`IngameWidget` when its inventory property activates. Gameplay entry uses
`CharacterSelectionCommand` operation 3 or successful character creation.
`tests/test_character_screen_logout.py` covers rapid relogs, late logout
packets, preservation of pending transfers, and normal Play routing.

## Scaling-difficulty access groups

The client may request an access-group ID rather than a physical map, for
example:

```text
a0203_kingscrypt02_03 -> a0203_kingscrypt02
```

`map_difficulty_state.resolve_map_access` uses:

- `_Template_MapAccessGroup.AccessGroup`
- `_Template_MapAccessGroup.Difficulty`
- `_Template_MapDifficulty.MonsterLevelPlus`
- `_Template_DifficultyFactors`
- `_Template_MapTemplate.IgnoreDifficultySelection`

The example access group selects map difficulty `3`. At player level 100 its
monster level is `100 + 15 = 115`, matching the captured live flow. The
physical map ID is used for map assets and routing; the requested access ID,
map difficulty, and scaling difficulty are retained in account state and on
the armed route.

`MapLoadPacket` keeps those two identities separate, matching the retail
capture: its first string is the physical map and its second string is the
selected access ID. A selected access variant uses ruleset `0`; repeating the
physical ID in both fields leaves `ClientGauntletManager` without a valid map
difficulty. Heroic physical IDs entered directly, such as by `!map` or an old
persisted state, are promoted to their difficulty-1 access row because the
Heroic difficulty-zero staging row maps to the client's
`InvalidMapDifficulty` enum and crashes `GauntletScoringWidget`.

`local_monster_state` resolves level curves and difficulty factors when
building every monster, so health, shield, damage, armor, resistances, and the
wire monster level are produced from the selected difficulty.

The effective monster factor is the greater of the access-group map
difficulty and the optional scaling-slider difficulty. Access IDs such as
`a0303_wildcave_08` carry Bloodshed in the former field while the travel
request's slider field remains zero. Bloodshed therefore uses database factor
8 (`MonsterHP = 21`) rather than factor 0; the final available tier has at
least ten times the same-level baseline health without hard-coding individual
maps.

## Sticker-book ownership

New characters have an empty collection and no mount shortcut. Empty saved
collections remain empty; owned mounts continue to be restored normally.

The database is authoritative:

- `_Template_StickerbookItem.Items` maps an unlock to source inventory items.
- `_Template_Item` supplies item category, effect, duration, and values.
- `_Template_StatusEffect` supplies wire effect identity, exclusivity, and
  modifier programs.

`AddStickerBookFromInventoryCommand` (`0x0133`) consumes the exact inventory
item and replies with two `InventoryInfo` transitions followed by
`StickerbookItemInfo` (`0x0130`, operation `1`). Ownership is persisted.
Source items for already-owned stickers are removed from NPC offer lists.

`_Template_Stickerbook.StickerbookRewardItems` lists each subcategory's rewards
as `threshold:reward:amount`, reached by the number of owned items in that
subcategory. A status effect reward applies for as long as the threshold holds
(`reconcile_collector_effects`). It only raises an actor variable; the bonus
comes from the pet, mount or Shadow Soul effect that reads it, so it needs one
of those active. An item reward is paid once, without a claim, when the
threshold is reached, and on map entry for anything reached earlier or that did
not fit the bags (`_grant_stickerbook_rewards`, saved as `stickerbook_rewards`).

`StickerbookItemInfo` also serializes its generated trailing resource string,
even when that value is empty for operation `1`. This two-byte empty-string
length precedes the actor context; omitting it shifts the rest of the batch
and produces the misleading state where an entry is visible but marked
unavailable.

`Items` is a semicolon-separated alias list. All listed base items are valid
unlock sources, including `vanity_handkiss_01;emote_handkiss_01`. Owning either
alias permits use and prevents consuming another copy to unlock the same entry.
Rarity upgrades remain separate tiers, not base aliases.

## Emotes

Stickerbook category 2 identifies emotes even though their inventory category is
`Consumable`. Using an owned emote through `0x0135` applies its authored finite
status effect without consuming inventory or ownership. Handkiss uses
`emote_love` (effect index 762), the item's 140-tick duration, and its authored
values. The client starts the animation from that effect's `StartAnimation` and
`StartSequence` metadata.

Playback uses the common timed status pipeline, broadcasts to nearby players,
and expires from runtime state. Repeating an emote starts a new full-duration
instance and cancels the old expiry timer. Emotes dismount the player and are
not saved as permanent costume/pet effects. Consumable-only emote variants are
still excluded from stickerbook unlocks.

`dso_server/tests/test_emotes.py` covers unlock packets, persistence requests,
alias ownership, local/remote animation effects, expiry, repeated use, unowned
requests, and separation from pet upgrade tiers.

## Mounts, costumes, and pets

`UseStickerBookItemCommand` (`0x0135`) is accepted only for an owned resource.

- Mounts use their database cast duration, transition from cast to ride, and
  persist across map handoffs. Cast and ride are both in the `Mount` status
  group, so `StatusEffectGroupDuration:...,relative,Mount` shortens them: the
  Undefeatables and Gwenfara cloaks (-50%) and Wisdom's mount talent (-3% to
  -31%). The amounts add up, as the client's own modifier does, from the
  item's 3.5 s and the ride's 36000 s.
- Costumes use the `Skin` exclusive group. Selecting the active costume again
  removes it.
- Pets use the `Pet` exclusive group. Selecting another pet adds the new
  effect and removes the previous effect without clearing unrelated status.
- Costume and pet effect instances are persisted and restored during player
  initialization.
- Pet status-effect values and modifier programs are part of the active
  combat modifier sources, so their stat benefits participate in server-side
  skill resolution.

Legacy/event mounts with no `_Template_StickerbookItem` row remain reusable
inventory items. Inventory operation `0x13` resolves the item's
`StatusEffectId`, follows the cast effect's `DoneModifiers` to its ride
effect, and uses the same timed mount state machine without consuming the
item. Quickslot assignment operations never activate the mount.

The server sends typed status-effect commands and never replays the recorded
packet captures.

## Decorative effects

Stickerbook category 6 uses separate decoration slots keyed by the status
`ExclusiveGroup`. `EffectCostume` auras, `VanityTrail` trails, and
`VanityGroundmarker` markers coexist with each other, a regular costume, and a
pet. Equipping a decoration replaces only the previous effect in its own group;
selecting it again removes only that decoration. The authored group takes
precedence over the stickerbook subcategory (for example, `e_bgh_effect` is
listed with effect costumes but occupies the trail group).

Active decoration resources are saved separately from the active costume.
Initialization rebuilds their effect instances and includes them in remote
appearance snapshots. Older saves with a decoration in `active_costume` are
migrated into the appropriate decoration slot when loaded.

## Player overhead icons

`UpdateShowPlayerOverheadIconsCommand` (`0x0023`) carries a bool followed by
the local actor id, with no additional actor context on client requests.
The captured `8b23008080000000` decodes to visible=true, actor=1 (57 bits,
or 64 bits with zero byte padding). The native settings sender is
`0x1401b81f0`; decoder `0x14093edb0` reads fields +0x20/+0x24, and handler
`0x1401cc58c` updates the addressed GamePlayer's +0x6a0 visibility flag.

Both gameplay routes accept changes only for the authenticated local actor,
retain the setting for the session, echo it locally, and remap the actor id
for peers in the same instance. Presence announcements and actor refreshes
include the explicit setting; PvP updates preserve it. Server replies include
both the command's actor field and the additional ServerCommand actor context.
`tests/test_player_overhead_icons.py` covers the capture, both toggle values,
late arrivals, instance isolation, PvP refreshes, and malformed requests.

## Remote overhead titles

`SetTitleCommand` (`0x0115`) persists the selected hidden, achievement, or PvP
rank mode and refreshes `RemotePlayerInfo` for existing peers. Achievement
mode with no selected achievement is normalized to hidden; advertising an
empty achievement key makes the client render
`LF:db.achievementtitles.table_Title`. PvP mode supplies the valid
`pvptitle00` localization id when the character has no earned rank. A
character may therefore legitimately display no title.

## Regression coverage

`tests/test_map_and_customization.py` verifies:

- access-group to physical-map resolution;
- level-115 monster construction for the captured difficulty-3 example;
- handoff metadata for a scaled map;
- inventory consumption and exact `0x0054, 0x0054, 0x0130` response order;
- costume persistence alongside a pet;
- replacement/removal of an old pet effect;
- pet modifier values from the shared database.

The broad suite also verifies that movement near an exit does not initiate a
transfer and that a real `0x0060` click still does.

## Costumes and pets used from the bag

`Switchable`, `Costume`, `TempCostume` and `Pet` items toggle their cosmetic
when used directly from the inventory (operation `0x13`): the first use wears
or summons it, the second takes it off, and the item is never consumed. The
toggle is the same one the sticker book drives. `load_cosmetic_definition`
resolves the cosmetic through the sticker book first, so an owned collectible
keeps its book identity, and otherwise from the item's own `StatusEffectId`;
263 of the 584 `Switchable` costumes are not collectibles at all. The active
costume is persisted by item template and restored the same way at
initialization.

## Symbolic `Hub` travel destination

The five-second "teleport to the last visited city hub" talent sends a
`TravelCommand` whose map is literally `Hub` (travel object `Talent`).
`_resolve_hub_travel_destination` turns it into a real map: the hub the
character last stood in (`last_visited_town`, tracked for respawn-to-town),
then the nearest ruleset-5 hub reachable from the current map through
authored exits, then Kingshill. The player arrives at that hub's default
travel-stone spawn. Ordinary destinations are unaffected.



## Tutorial relog destination

Characters saved in the tutorial dungeon, garden, tomb, or fortress return to
`a0002_start_hub` on relog. Their saved dungeon position and difficulty are
cleared so they use the hub's normal entry; quest progress is retained.
The tutorial's quest-gated exits are not a reliable login-time hub graph:
without their map events the old search fell back to Kingscity. A saved hub
login keeps its position, and new characters without a saved world state
still begin in `a0001_start_tutorial_dun`.
