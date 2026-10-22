# Catacombs and Crypt of Kings exit visibility

## Confirmed behavior

Sending `ExitInfo` and changing its order relative to `EventUpdate` did not
create the missing exits on this client. The client log reported
`ClientGameWorld::UpdateExitState() exit entity ... not found` for both the
Catacombs connection and the Heredur boss entrance. A valid outgoing exit
snapshot is not evidence that the scene entity exists.

The zero-GUID `t000_exit_general_01` StateObject fallback was confirmed visible
in game. It was kept during the first native creation check. Its temporary
slots were allocated after the authored props:

| Map | Destination | Prop ID | Authored position |
| --- | --- | --- | --- |
| `a0202_kingscrypt01` | Crypt of Kings | 65985 | (52, 4, -174) |
| `a0203_kingscrypt02` | Catacombs | 65983 | (44, 8, -174) |
| `a0203_kingscrypt02` | Heredur | 65984 | (-10, -4, -32) |

The user confirmed all three gates visible after this restoration. The
generic StateObject lacked the native exit label, minimap icon and difficulty
selection behavior; its server interaction transferred directly to the
destination. It must not be considered a complete exit implementation.

## Explicit native creation

The running release261_192 client implements `Commands::VariationInfoCommand`
(`0x0043`). Unlike `ExitInfo`, its handler factory-creates missing map entities
by GUID. This path was absent from the server.

Binary evidence from the running `dro_client64.exe`:

- Command decode: `0x14097d91c`, array decode `0x14095ceb8`.
- Record decode: `0x140a96a58`; encode `0x140a96a90`.
- Command handler: `0x1401d4ce0`; looks up the GUID, then uses FactoryManager
  to create and EntityManager to attach an absent entity.
- `ClientEventSystem::IsGameObjectActive`: `0x1401fba74`. EventSetRow `-1`
  returns true, permitting these unconditional base-map exits.

The body is a uint32 count followed by records of a uint16-length GUID blob
and a signed int32 EventSetRow. It has the standard trailing actor ID. The
in-memory identity transform and ParentNodeIndex are not serialized.

Initialization now primes the exit cache, sends one VariationInfo record for
each selected destination family, sends EventUpdate, then refreshes ExitInfo
after the native entities have been created. Only the current difficulty's
GUID is requested for each physical gate. Crypt of Kings includes both its
return gate and its boss gate.

The client cache's map DB was read without modification and its exit GUIDs
match the server map DB. Native entities retain the authored ExitURL and
`ui/icon_map_exit` metadata.

The user then confirmed the minimap icon and difficulty screen working with
explicit native creation enabled. The server log recorded native client
`0x0060` requests in both directions at 17:49:36 and 17:49:43 and through the
Heredur entrance at 17:50:07 on October 22, 2026. These requests had previously
been bypassed by the fallback click
handler. The client log's post-creation snapshots no longer reported the
selected `_00` exits missing; unselected tier aliases can still produce
lookup warnings.

After this in-game confirmation, the temporary StateObject visuals and their
direct-transfer handler were removed. The final implementation uses actual
InteractExit scene entities. Do not regress to sending only ExitInfo: explicit
VariationInfo creation is the material difference from the failed attempts.

Focused checks:

```
python -m pytest tests/test_crypt_exit_visibility.py tests/test_grimmagstone_exit.py -q
```

The checks cover native wire framing, all seven crypt difficulties, both
Crypt of Kings destinations, creation/cache-update ordering, authored minimap
metadata, routing the difficulty selected by the client, and existing
Parallel World exit behavior. Protocol tests alone cannot prove visual
appearance or the client difficulty widget; the in-game confirmation above
is separate evidence.
