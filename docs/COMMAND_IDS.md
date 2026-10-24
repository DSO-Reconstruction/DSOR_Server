# Recovering the client's command id table

## Server default: `release261_203`

The server uses [`command_ids_rel261_203.json`](command_ids_rel261_203.json) by

default, matching the new `rel261_203` client. The earlier `rel261_192` table
remains available for explicit use through `DSO_COMMAND_IDS`.

This table and the corresponding marker-bit defaults were adapted from

[upstream commit a2cfa7d](https://github.com/shockblaze/DSOR_Server_Kitty261/commit/a2cfa7d308ade059c788f1ef0903b27a640375de).

For `dro_client64-patch5` or the DSOR client, set

`DSO_COMMAND_IDS=builtin` and `DSO_SKILL_HIT_MARKER_BITS=0`.

## rel261_203 update analysis

The October 2026 client update was analyzed from

`%LOCALAPPDATA%\Temp\DSOClient\dlcache\dro_client64.exe` (the embedded

revision marker is `261_203`; SHA-256

`e0fe05f0f3746bc8ae55927958d1dd93641eb1948ccdd9d4f474da93dac49f81`). The

recovered table is in

[`command_ids_rel261_203.json`](command_ids_rel261_203.json). It contains the

same 371 command class names as rel261_192, so no new registered commands were

found. The class-to-id change is:

| command class              |   rel261_192 |    rel261_203 | note                 |
| -------------------------- | -----------: | ------------: | -------------------- |
| `DebugSpecialOfferCommand` | `0x84` (132) | `0x107` (263) | moved to a unique id |

`SpecialOfferCommand` remains at `0x84` (132). The rel261_192 source table had

both classes assigned to 132; rel261_203 assigns distinct ids. The new table

has 371 classes, 371 unique ids spanning 1–381, and no duplicate ids. Its

actor-scope flags were recovered from vtable slots 13 and 14 and match the

rel261_192 flags for all 371 classes (299 actor-scoped, 72 unscoped).

Recovery followed each command's RTTI class-name registration to its creator,

followed the creator to the constructor, read the constructor's vtable, then

read `GetCommandId()` from vtable slot 3. This is the same chain described in

§2 below, applied to the rel261_203 x64 executable. The table is selected by

default; `DSO_COMMAND_IDS` can select another supported client table.

The detailed command recovery notes below document the patch5 table in

[`command_ids.json`](command_ids.json).

The complete network command id → class map for

`dro_client64-patch5.exe`: **341 classes, ids 1–368, no duplicates.**

> **Namespaces.** The first version of this scan matched only
>
> `Commands::…Command` and recovered 273 classes, leaving a conspicuous hole at
>
> ids 0xC0–0xDA. Command classes live in six namespaces, and the hole was the
>
> whole `PVP::` block. Widening `COMMAND_NAME` to `<NS>::…Command` recovers all
>
> of them:
>
> | namespace       | classes | notable range                                   |
> | --------------- | ------: | ----------------------------------------------- |
> | `Commands`      |     273 | everywhere                                      |
> | `PVP`           |      28 | **0xC0–0xDA**, plus `ResetMatchQueues` at 0x13C |
> | `Ranking`       |      21 | 0xEB–0xFF                                       |
> | `StatusEffects` |       9 | includes `StatusEffectCommand` = 0x4F           |
> | `Talents`       |       8 |                                                 |
> | `Achievements`  |       2 |                                                 |
>
> This also resolved two of the six ids that previously had "no class at this
>
> id": `0x4F` is `StatusEffects::StatusEffectCommand` and `0x3E` is
>
> `StatusEffects::LocationEffectInfoCommand`, both of which the repo had
>
> already labelled correctly from the wire. See
>
> [PVP\_MATCHMAKING.md](PVP_MATCHMAKING.md) for what the `PVP::` block turned
>
> out to contain.

Produced by [dump\_command\_ids.py](../../dump_command_ids.py); the full table is

in [command\_ids.json](command_ids.json), beside this file.

Run from the repository root:

```
python dump_command_ids.py --json dso_server/docs/command_ids.json --check
python dump_command_ids.py --grep Stickerbook
```

---

## 1. Why the obvious approach fails

The client dispatches commands through

```
Network::CommandFactory<unsigned short, 369>::RegisterCommand(
    class Network::Command *(__cdecl *)(void), unsigned short)
```

so the plan was to dump every `RegisterCommand` call site and read its two

literal arguments. That does not work on this build, for two reasons found the

hard way:

- **`RegisterCommand` is inlined.** Its `__FUNCSIG__` string is present in

  `.rdata` but has **zero references** of any kind — no `lea`, no 64-bit

  immediate, no stored pointer. The assert that used it was compiled out and the

  string is orphaned. The same is true of

  `DrasaClientHandler::DecodeCommand(): Received command with invalid command
  id (ID: '%hu')!`, so the decode path cannot be anchored on either.
- **The factory is not a static array.** Scanning for the inlined store form

  (`lea rax,[rip+fn]; mov [rip+slot],rax`) finds 11 pairs image-wide, none

  clustered. The table is heap-allocated and filled at runtime.

Two further dead ends, recorded so nobody repeats them:

- Pattern-scanning for `lea rcx,[rip+fn]; mov edx,imm; call` yields 81 sites,

  none with a plausible id distribution. Widening it to an order-agnostic

  look-back window produces thousands of false positives — small integer

  constants near calls are extremely common, and the top hit (`0x140c9d318`,

  1254 sites) is just the logging helper.
- **MSVC RTTI is stripped** (`/GR-`): only 140 `.?AV…@@` type descriptors exist

  in the whole image and none is a command class, so vtables cannot be named

  that way. The client uses Nebula3's own RTTI instead.

## 2. The chain that works

Every command class carries its id in a virtual accessor, and Nebula3's

`__ImplementClass` registration provides a path to it:

```
Rtti init fn        lea rdx -> "Commands::XCommand"     class name string
                    lea r9  -> creator fn               4th ctor argument
                    lea rcx -> Rtti object              this
      │
creator fn          last call -> class constructor
      │
constructor         lea -> vtable, stored via mov [rbx], rax
      │
vtable slot 3       mov eax, <id> ; ret                 GetCommandId()
```

Worked example — `Commands::ActorRequestCommand`:

```
init fn   0x1400a7208   lea r9,  0x140946a00      creator
                        lea rdx, 0x1411230c0      "Commands::ActorRequestCommand"
                        lea rcx, 0x141394840      Rtti object
creator   0x140946a00 → thunk → 0x140937934, last call → 0x14092ccac
ctor      0x14092ccac   lea rax, 0x141125050 ; mov [rbx], rax      vtable
vtable    0x141125050   slot 3 → 0x140946ff8: b8 1c 000000 c3      mov eax,28; ret
```

`28` = `0x1C`, which is exactly the `ACTOR_REQUEST_COMMAND` the server already

had. Slot 3 is `GetCommandId()` in every command vtable.

Function boundaries come from the PE's `.pdata` exception directory (75 193

`RUNTIME_FUNCTION` entries), which makes attribution exact rather than

heuristic. No disassembler is needed — only `lea r64,[rip+disp32]`,

`call rel32`, `jmp rel32` and `mov eax,imm32; ret` have to be decoded.

### Second pass: classes without a creator

Six classes register with no creator argument, so they are not

factory-instantiable and the chain above stops at step 1:

`GuildInvite`, `InventoryInfo`, `RedeemVoucherResponse`, `RevenueRecognition`,

`Travel`, `UnlockMap`.

For these, the fallback searches for any code referencing the class's Rtti

object and follows its calls looking for a vtable whose slot 3 is a constant

stub. All six resolve — and two of them, `TravelCommand` = `0x61` and

`UnlockMapCommand` = `0x60`, match ids the server already had, which is a useful

independent check on the fallback specifically.

## 3. Validation

Cross-checked against every command id hard-coded in `gameplay_protocol.py`

(`--check`):

**35 exact name matches, 5 same-id-different-label, 0 contradictions.**

The label-only differences are the repo's naming, not disagreements:

| repo constant                       | client class                            |
| ----------------------------------- | --------------------------------------- |
| `GUILD_MOTD_REQUEST_COMMAND`        | `GuildMessageOfTheDayRequestCommand`    |
| `CLIENT_AVERAGE_FRAME_INFO_COMMAND` | `ClientAverageFrameInfoTrackingCommand` |
| `SET_STICKER_BOOK_FAVORITE_COMMAND` | `UpdateStickerbookFavoriteCommand`      |
| `INVENTORY_ITEM_ACTION_COMMAND`     | `InventoryCommand`                      |

Six repo constants have no class at that id — including

`MOVE_COMMAND 0x5F` and `STATUS_EFFECT_COMMAND 0x4F`. Note there is \*\*no

`Commands::MoveCommand` string in the binary at all\*\*, so those names are the

repo's own labels for ids observed on the wire; the real classes are either

named differently or are server→client-only types that the client never

registers. This is a naming gap, not a contradiction.

Structural checks that also hold:

- ids span **1..368**, fitting `CommandFactory<unsigned short, 369>` exactly

  (slots 0–368);
- **no duplicate ids** across 341 classes;
- the nine quest commands occupy a contiguous `0x8A–0x91` block plus one

  outlier, matching the subsystem clustering visible elsewhere

  (`0x26/0x28/0x29` NPC, `0x2A/0x2B/0x2C` monster, `0x36/0x38` props).

**369 is the table capacity, not the command count.** 341 classes are

registered; the remaining 28 slots are unused or belong to server-only

commands the client never constructs.

## 4. What this unblocks

- **Quests** — the nine opcodes were the last open item in

  [QUEST\_FEASIBILITY.md](QUEST_FEASIBILITY.md).
- **Every other unmapped command.** The server currently hard-codes ~46 ids;

  this yields 341 with names, including whole subsystems not yet touched

  (season pass, gauntlet, daily challenges, stickerbook, wanted missions).
- **PvP matchmaking**, which the `PVP::` namespace made addressable and which

  is now implemented — see [PVP\_MATCHMAKING.md](PVP_MATCHMAKING.md).
- **Naming.** `gameplay_protocol.py` constants can be reconciled against the

  client's own class names, and the four label mismatches above corrected or

  annotated.

## 5. Recovering a command's field layout from its vtable

The id table gives each class's vtable, and that is enough to read the

generated serializers rather than guess a layout from captures. In the same

vtable:


|   slot | meaning                                   |
| -----: | ----------------------------------------- |
|      3 | `GetCommandId()` — `mov eax, imm32 ; ret` |
|      4 | `Encode`                                  |
|      5 | `Decode`                                  |
| 13, 14 | outer server-command actor accessors      |

`Decode` is a flat sequence of `call <reader>` with `lea rdx, [this + offset]`,  
so the field order and each field's width read straight off the disassembly.  
The readers are shared and identifiable by size: 45 bytes is a `uint32`, 188 a  
string, 53 a *validated* enum, and 230 the `uint32 count` + element array used  
by `PlayerQueryResultCommand`, `GroupStatusCommand`, and  
`GroupMemberQueryResultCommand` alike — which is how those three were confirmed  
to share one `PlayerSummary` record.


A validated enum reader ends in `add cl, 1 ; cmp cl, N ; ja fail`, giving the
accepted range as `-1 .. N-2`. Anything outside it aborts the *whole* command,
so a server that sends an out-of-range value sees the client silently drop the
packet. `GroupingFailedCommand`'s reason is `-1..3`;
`GroupMemberQueryResultCommand`'s status is `-1..1`.

**Slots 13 and 14 are the actor-context discriminator.** Commands that carry a
trailing `uint32` actor after their body override them with real functions;
commands that do not leave the inherited stubs (`0x140a3ff34` / `0x140a3ff70`
in this build). This is checkable in seconds and settles a question that
otherwise needs trial and error against a live client:

| command | slots 13/14 | trailing actor |
| --- | --- | --- |
| `GroupInvitationResponseCommand`, `GuildInvitationResponseCommand` | overridden | yes |
| `GroupStatusCommand`, `PlayerQueryResultCommand`, `GroupFoundationResponseCommand` | stubs | no |
| `GroupingFailedCommand`, `GroupMemberQueryResultCommand` | stubs | no |

`command_ids.json` now carries this as an `actor_scoped` flag on every class,
so a codec no longer has to re-derive it: **270 of 341 classes carry the actor,
71 do not.** The two overriding functions read and write a `uint32` at object
offset **+0x18**, which gives a second, independent tell — a wrapped class
declares its own first member at +0x20 because +0x18 belongs to the wrapper,
while an unwrapped one starts at +0x18.

Getting this wrong is not a silent truncation, and it is worth knowing what it
looks like from the outside. Omitting the actor on a wrapped command makes the
client log

```
RakNetStream::ReadBits(): error while reading stream!
DrasaClientHandler::DecodeCommand(): Could not decode command's additional
    server data (ID: '207', 'PVP::JoinMatchQueueResponseCommand')!
DrasaClientHandler::HandleServerCommand(): DecodeCommand() returned invalid
    ptr for cmd id 207!
```

and drop the command — leaving its bit reader mid-stream, so every command
batched behind it decodes as garbage. The visible symptom is therefore usually
*not* at the offending command but somewhere later in the same batch.

Client→server operation enums are recoverable the same way, from the property
that builds the command rather than from the command itself.
`NetworkCommandCreatorProperty::HandleMoveItem` and `HandleSplitStack` each
select an operation by which destination-slot field the UI message carries, in
`Game::Inventory` container order — which is how the inventory move (`6..0xa`)
and stack-split (`0x1e..0x21`) operations were recovered. Their funcsig strings
are in `.rdata` and each function references its own, so a `lea` xref scan
finds them.

## 6. Limits

- Ids are read from `GetCommandId()`, i.e. what each class *reports*. That is
  the value the factory is keyed on, but it is one inference step away from
  observing the id on the wire. The 35 wire-confirmed agreements make a
  systematic error unlikely.
- Slot 3 being `GetCommandId()` is derived from this build's vtable layout. If
  the base class changes, `VTABLE_ID_SLOT` in the tool needs revisiting — a
  mismatch would show up immediately as `--check` failures.
- The tool is specific to this client build. Re-run it after any client patch
  rather than trusting a stale [command_ids.json](command_ids.json).


## Extended group popup and friends-list refresh

`GroupMemberQueryCommand` (`0x00b8`) carries a uint32 request context followed
by the **selected group ID**. The native query builder at `0x14027245c` writes
the selection to command offset `+0x24`. ClientSocialManager's `+0x228` field
is the group selected in the finder popup, not the player's own party;
`0x1402628fc` replaces it by copying a `GroupInfo` record.

`GroupMemberQueryResultCommand` (`0x00b9`) contains an 8-bit status, uint32
request context, uint32 selected group ID, then an array of `PlayerSummary`.
There is no trailing actor context. The native handler at `0x14026ac10`
accepts status 0 with a nonempty roster matching the selected group. Status 1
with an empty roster invokes `0x14052931c` to clear/close the popup, so a group
that disappears between listing and inspection must receive a failure reply.
Earlier comments incorrectly treated the selected-group field as membership.

The server resolves the requested group independently of the viewer's own
membership. Other players can inspect groups exposed by the local finder;
members can also inspect their own unlisted group from another map. Inspection
does not send GroupStatus or change membership. Member information uses live
character metadata when online, current saved profiles when offline, and the
persisted social member as a fallback. Activity flags require an initialized,
connected gameplay session. Actor IDs are nonzero only in the viewer's map
instance (or for the local player).

The social window also sends `BuddyListCommand` (`0x00b0`): operation byte,
uint32 context, string, and boolean. The recorded empty refresh is 81 meaningful
bits, padded to 11 bytes. Operation 0 receives `BuddyStatusCommand` (`0x00b1`)
with status 0, uint32 context, and a PlayerSummary array. The current store has
no buddy relationships, so that array is empty. Friend additions/removals are
outside this refresh implementation and are not acknowledged as successful.

Native evidence is in `data/diagnostics/group-popup-native.txt` and
`data/diagnostics/group-popup-query.txt`. Regression coverage exercises both
gameplay routes, non-member and alternate-party inspection, full five-member
rosters, offline and changing presence, and unavailable-group failure replies.
