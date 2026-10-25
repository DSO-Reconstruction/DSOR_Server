# NPC interaction failures during initial map entry

## Symptom

An NPC is visible and the player can walk next to it, but clicking does not  
start an interaction. Leaving the character and reconnecting can make that  
same NPC clickable without changing the map data. This points first to a  
client/server actor-registration or initialization-order problem. Visibility  
alone does not prove that the client has a selectable NPC actor.

This pattern is distinct from an NPC that is absent, a quest that has not  
revealed it, or an NPC whose authored interaction is disabled.

## Interaction lifecycle

The client needs the map scene entity and its network actor to agree:

1. The server sends `ActorsEnterVicinity` (`0x0074`) with the NPC actor IDs.
2. `EventUpdate` (`0x00dd`) activates the map-local scene sets for this entry.
3. The server sends each NPC's `NewNPC` (`0x0026`) and `NPCInfo` (`0x0028`).
4. The client can then select the actor and send `NPCRequest` (`0x0029`) or  
   `EncounterCommand` (`0x0066`) with its actor ID.

The order matters. The vicinity announcement reserves the network actor ID;  
it does not replace the later `NewNPC` creation. If `NewNPC` arrives before  
the map event has materialized its scene entity, the scene model and network  
actor can be left out of sync. A later character reconnect rebuilds the  
client state and can hide the timing problem, which explains why a second  
entry may work while the first one does not.

## Current server ordering

The local initialization path currently uses this sequence:

- pre-register actor IDs in a separate reliable-ordered `0x84` frame before  
  the player initialization stream;
- process the map's `EventUpdate` in the initialization stream;
- send `NewNPC`/`NPCInfo` pairs after `EventUpdate` and before `PlayerReady`.

The `0x84` above is the outer RakNet datagram identifier, not the application
payload prefix. The historical fix (`c1ccbad`, included in `01bf9a2` for the
R261_203 update) uses `encode_actors_enter_vicinity_batch`: its application
payload starts with `0x85` and is sent in a separate reliable-ordered frame.
Do not replace that encoder with the standalone `0x84` application-message
encoder based on the datagram identifier alone.

Keep these frames reliable and ordered. Do not move NPC creation ahead of the  
event snapshot without client evidence that the client reserves the actor ID  
there and requires that ordering. Runtime event/quest spawns send their  
vicinity registration immediately before their `NewNPC` batch; preserve that  
pairing when changing those paths.

The ordering regression test is  
`MapLocalEventUpdateTests.test_kingshill_creates_npc_actors_after_event_snapshot`;  
the player-init test verifies that Kingshill NPC actor IDs are registered  
before the initialization batch.

## Diagnosing a recurrence

For the first failed click, record the physical map ID, difficulty, NPC  
template, whether the model is visible, and whether reconnecting changes the  
result. Compare the first entry with the reconnect path.

Check evidence in this order:

1. **Client target lookup.** `PlayerInputProperty::DeterminePrimaryActionOnTarget:
   npc not found` means the client could not resolve the selected entity to a  
   registered NPC actor. Capture this alongside the failed click.
2. **Server request.** Look for `NPCRequest references unknown local actor`,  
   `EncounterCommand`, or `RequestOffer` lines at the same time. If the client  
   reports `npc not found` and sends no actor request, changing shop/quest  
   resolution on the server will not fix the client-side selection.
3. **Initialization order.** Decode the outgoing sequence and confirm that  
   actor IDs are registered once, `EventUpdate` precedes `NewNPC`, and each  
   `NPCInfo` follows its matching `NewNPC`. All three must use the same actor  
   ID and map instance.
4. **Actor-ID uniqueness.** Check that NPC IDs are unique and do not overlap  
   other actors registered on the map, including props. Confirm prop ID  
   allocation starts after the map's reserved NPC actor range.
5. **NPC metadata.** Compare the affected `_Instance_NPC` row and its  
   `_Template_NPC` record with a working NPC. Check `NoTalk`, `Profession`,  
   `ShopId`, template ID, instance GUID, event-set row, and the emitted  
   `NPCInfo` category. A visible NPC with `NoTalk = 0` can still fail because  
   its network actor was not attached correctly.
6. **Database identity.** Compare server and client `static.db4` hashes when a  
   content mismatch is suspected. Matching files rule out a simple file  
   version difference but do not rule out bad server-side mapping or packet  
   order.
7. **Test both paths.** Verify a fresh first entry, reconnect, and a second  
   map transition. Reconnect success by itself is not a complete fix; the  
   first-entry path must also work.

Do not globally change NPC actor-ID allocation or edit files under `data/`  
until logs or database inspection demonstrate that the authored data is the  
cause. Prefer a regression test for the observed first-entry sequence and  
confirm the result in the client after the server-side checks pass.

## Active cache identity: confirmed Kingshill recurrence

On 2026-10-25, the server's Kingshill database matched the flat-cache
`export_win32_maps_a0200_kingscity.db4.unc`, but that was not the database
loaded by the running client. The active
`dlcache/export_win32/maps/__toc` selected the object with digest
`308ffaf269c1b1b8927780b4f93815c6`; the runtime file was
`dlcache/export_win32/maps/a0200_kingscity.db4`.

All 119 NPC rows had identical row IDs and metadata when columns were
compared by name, but every instance `Guid` differed. `NewNPC` therefore
referred to a different scene entity from the one the player could see.
Kingshill's active runtime database is selected by the map TOC; the flat
`.unc` sidecar can belong to an older cache generation. The server now reads
the selected client GUID by row ID for NPCs and state objects only when the
authored identity, template, transform, and event-set metadata also match.
This keeps `data/` unchanged and falls back to its GUID if the client cache
or matching row is unavailable. Crafting props use the same mapping in both
the player-init path and the map-event prop catalogue.

The server and client `static.db4` hashes differ, but the 108 NPC templates
used by Kingshill exist in both databases with identical fields. That static
database difference is not the cause of the missing Kingshill NPCs.

For a recurrence, compare the actual runtime database and its active TOC,
not just an unversioned `.unc` sidecar. Multiple cache generations can coexist.
Compare GUIDs as well as file hashes, and compare SQL columns by name because
their physical ordering can differ between builds. The cross-build GUID
issue is also described in `services/content.py` and commit `d538b86`, which
publishes map databases for the DSOR client.

## Verification status

The initialization ordering described here has automated protocol coverage.  
Automated tests confirm command ordering and actor-ID consistency, but they  
cannot prove that the client makes the NPC clickable. After any ordering  
change, verify the first-entry behavior in the game and retain the relevant  
client and server log excerpts.
