# experimental

Notes on the Drakensang Online network protocol, written while building a server
the retail client will talk to.

The client logs in, is dispatched to the character service, is shown its character
at the level and experience it was last saved with, is granted the game, returns to the
login server, is dispatched to a map server and enters the world. Creatures spawn around
it, walk toward it, strike it with an animation, take damage, die and drop loot.
Experience is awarded and levels are granted. Skills put status effects on their caster
and their victims, with the visual effect, animation and icon the client draws from its
own database. The action bar survives a restart. The shop shows whatever this server
decides, at whatever price.

Four sources feed these notes:

* **packet captures**, of real sessions and of this server's own;
* **the client's log**, which names most of its own refusals;
* **the client's binary**, a native Windows x64 build. Command ids come from
  walking the Nebula3 Rtti registration to the id getter at vtable slot 3
  (`tools/command_ids.py`); function bounds come from the PE exception table;
* **the client's database**, `static.db4` — plain SQLite despite the extension, 197
  tables and 148,265 rows, loaded into memory at startup (`dsor/database.py`).
  `_Template_Skill` and `_Template_Monster` hold the numbers that govern combat — hit
  frames, ranges, cooldowns, damage types, which skills a monster carries;
  `_Template_StatusEffect` holds every effect and the sequence, animation and icon the
  client draws for it. A row's wire index is **not** the same expression for every
  table, and getting that wrong cost more than anything else here — see
  [the wire index](#the-wire-index-of-an-effect-is-rowid--15).

Guesses say they are guesses. Ideas that turned out to be wrong are kept under
**Refuted** rather than deleted, because most of them are plausible enough to be
tried again.

## Running it

You need Python 3.11 or newer and nothing else — no third-party packages to serve a
client, `pytest` to run the tests, `frida` only for client-side capture. This
repository contains no game files.

```sh
python server.py --advertise 172.20.0.1 -v --capture ~/session.jsonl
```

`--advertise` is the address written into every handoff, and it has to be reachable
**by the client**. Behind a VM that is not an address this process can see on itself,
so it cannot be detected and must be given. `--capture` records every datagram in the
same format as a reference capture, which is what makes a failing session diffable
against a working one — use it.

Then point the client at it. The one argument that matters is `-ip`; nothing needs
patching. `tools/frida/hook-dso.ps1` replaces it and attaches Frida in one step:

```powershell
powershell -ExecutionPolicy Bypass -File \\host.lan\Data\hook-dso.ps1 `
  -Spawn -SocketOnly -CmdFile \\host.lan\Data\dso\cmd.txt -LoginIp 172.20.0.1:2190
```

`-SocketOnly` skips the function hook and records UDP payloads only, which is the
reliable mode; drop it to also hook a function by offset. `-ShowArgs` prints a running
client's command line, which is how you produce the `cmd.txt` that file expects.
**That file holds a live session credential and a CDN key** — the `.gitignore` here
already excludes it.

A fight, with everything on:

```sh
python server.py --advertise 172.20.0.1 -v     --mobs 6 --mob-first-command --creature-damage 3     --shop-offers 1 --shop-price 0
```

| flag | what it does |
|---|---|
| `--mobs N` | place N recorded creatures, up to 6 |
| `--mob-first-command` | answer an entity request with the creature's own commands rather than the whole recorded batch |
| `--mob-damage D` `--mob-health H` | how hard the player hits and how much a creature has. Both invented: no capture reports a creature's health |
| `--creature-damage D` | how hard creatures hit back, `0` to disable |
| `--mob-patrol N` `--mob-near D` | move creatures about, or place them near the player. Both go beyond what any capture shows, and `--mob-near` currently stops them appearing |
| `--shop-offers N` `--shop-price P` | serve N purchase offers at price P. The cheapest proof a format is generated rather than replayed |
| `--schedule-delay S` | how long to hold the 717 KB event schedule back. Zero reproduces the crash it exists to avoid |
| `--map` | which map the map server serves |
| `--login-port` `--character-port` `--map-port` | for running two instances side by side |

And without a client at all:

```sh
python -m pytest -q
```

The suite needs no game and no network. Two of its files replay a recorded client
into a real server in-process — one for the character tier, one for the map tier — and
those are where a fault should be looked for first. `docs/REPRODUCING.md` covers
reading the four artefacts a session produces and the three traps in comparing them.

## Topology

Three tiers, and the surprising part is the first one: **the login server is a
persistent dispatcher**, not a one-shot authenticator. The client returns to it
before every zone change — eleven separate connections in one fifteen-minute
session — and it issues every handoff.

| port | service name | role |
|---|---|---|
| 2190 | `DrasaOnlineLoginServer` | dispatcher, contacted before each zone change |
| 2191 | (chat) | not investigated |
| 2192 | `DrasaCharacterService` | selection screen only; serves the map `a0000_char` |
| dynamic (e.g. 30201) | `DrasaOnlineMapServer` | one world map |

The character service does not serve a playable map. It grants the client's request
and hands it back with an **empty** handoff, and the client returns to the login
server for a real destination.

## Command names

`docs/commands.md` has all 365, generated by `tools/command_ids.py`. What follows
is the handful this server actually uses.

The client is a native C++ build with no RTTI for its own classes, but Nebula3
registers every class through a `Core::Rtti` constructor called with the name as a
literal — 2282 of them — and each class's id getter sits at vtable slot 3 as a
two-instruction `mov eax, <id>; ret`. That yields **372 command ids paired with
their class names**, with no duplicates on either side, and the ids form a
contiguous run from 0x0001 to 0x0176 with three gaps. The client's own factory
refuses anything at or above 375, which confirms the space independently.

The ones this server needs:

| id | class | direction |
|---|---|---|
| `0x001C` | `ActorRequestCommand` | client asks what an actor is |
| `0x001D` | `NewPlayerCommand` | |
| `0x002A` | `NewMonsterCommand` | creates a creature |
| `0x002B` | `DiscardMonsterCommand` | never seen on the wire |
| `0x002C` | `MonsterUpdateCommand` | stats, not position |
| `0x004F` | `StatusEffectCommand` | |
| `0x005F` | `MoveCommand` | positions, both directions |
| `0x006B` | `HitCommand` | |
| `0x0070` | `SwitchMapCommand` | the handoff |
| `0x0073` | `ActorsLeftVicinityCommand` | |
| `0x0074` | `ActorsEnterVicinityCommand` | |
| `0x007B` | `ActorStatsUpdateCommand` | |
| `0x0086` | `CharacterGenerationCommand` | character *creation* |
| `0x0087` | `CharacterSelectionCommand` | selection and entering the world |
| `0x00A7` | `GuildStatusCommand` | |
| `0x0114` | `AchievementInfoCommand` | |
| `0x010B` `0x010C` `0x010E` | `OCPOfferRequest` / `Methods` / `OfferResponse` | the shop |

Reading these names first, rather than guessing from sizes and positions, is what
unblocked this project every time it stalled. Every wrong turn recorded under
**Refuted** below was a guess made while the answer sat in the binary.

## Commands come in batches

A datagram does not carry one message. The client reads commands in sequence until
fewer than sixteen bits remain, and **each command ends with a `0xFF` terminator**
followed immediately by the next command's 16-bit id.

This is easy to misread. A multi-entity position update looks
exactly like records joined by a two-byte separator `5F 00` — and that reading
produces byte-identical output, so it survives every test. It is wrong: the `0xFF`
closes each command and `5F 00` is simply the next `MoveCommand`'s id. The mistake
only surfaced on reading the client's decode loop.

Two consequences worth keeping in mind. Per-id byte counts taken from a capture are
**batch sizes labelled by the first command's id**, not message sizes — which is why
the 631 KB "`0x001D`" holds some 1250 identifiers. And a recorded batch cannot be
replayed wholesale onto a different world state: it carries the whole cascade the
original session produced, including positions and loot placement. Filtering a batch
to the commands whose trailer names the actor you care about, plus those addressed to
no actor at all, is the workable middle ground between replaying everything and
replaying only its first command.

## A creature, from nothing to fighting

Three legs, and only the last two are obvious:

```
server → client   0x0074 ActorsEnterVicinityCommand   an actor is near you
client → server   0x001C ActorRequestCommand          what is actor N?
server → client   0x002A NewMonsterCommand            this is what it is
```

A position update **alone will not do it**. `HandleMoveCommand` asks
`CheckActorNeedsToBeRequested` first, and for an actor the client has never heard of
it discards the command outright and asks. Leave that question unanswered and the
creature never exists — the map loads, the player walks around, and nothing appears,
with not one line in the client's log. It keeps asking for as long as it runs: 136
times against this server before the answer existed, against 8 in a whole real
session. Counting *distinct* requested ids is what tells retries apart from new
actors.

`0x002A` also pins the creature's initial position and health, as three floats and an
int64 pair inside its own payload. And `RequestActor` is called directly from the
vicinity handler, so an actor announced that way is set up by the path the real
server used, rather than one the client merely noticed.

## Serving a creature that was never captured here

A capture of a full tutorial run yields complete `NewMonsterCommand`s for eleven
creature blueprints across three maps. Each is a single command rather than a batch,
and serving one somewhere else needs exactly two fields rewritten:

* **its actor id**, in the trailer. That is the actor the client creates, so serving a
  description under a different slot without changing it creates the wrong actor — and
  the client goes on asking about the one it wanted, every three seconds, indefinitely.
  A request that repeats is a request never satisfied, which is the same signature as
  one never answered at all.
* **its spawn position**, 448 bits from the end. A description carries the position of
  wherever it was captured: a third-map creature served untouched on the first map
  appears a hundred units away, correctly and invisibly. That is not a failure mode
  that looks like anything — no rejection, no log line, no repeat.

Both offsets are rules rather than tables, and the position's was found by
cross-reference rather than search: three creatures appear both as a batch, where the
offset was already known, and as a single command, so searching each single command
for the float triple its batch carries locates it. 448 bits in all three, at three
different message lengths.

Rewriting only the blueprint *name* does not work. Everything behind it — including a
second field holding an array of ten composite elements of variable size — still
describes the original creature, so the name resolves and nothing is drawn.

## A creature has two positions, in two frames

* Its **description** carries three 32-bit floats, in world units.
* Its **movement records** carry three signed 16-bit fields, world units × 128.

They are not the same frame. Measured over one session: the player's decoded
trajectory runs z=46 down to z=21, while the descriptions place the creatures at
z=52 to 55 — an interval the player never enters. Measured against the descriptions,
the player was never nearer than 18 units and was 30 away at the moment of every
single attack. Measured on the wire, the same player came within 0.9.

So a distance bound of 1.75 refused every blow, and so did a bound of 6. Compare wire
against wire.

## Combat

`MoveCommand`, from the client's own decoder — and this is the layout, not a reading
of ours:

| offset | width | field |
|---|---|---|
| 0–5 | 3 × int16 | position, world units × 128 |
| 6 | uint8 | scaled by 1/64 |
| 7–8 | 2 × uint8 | two angles, each `(v/256)·2π − π` |
| 9–12 | uint32 | start tick |
| 13–14 | uint16 | duration; the end tick is the sum |
| 15–18 | uint32 | actor id, from the command's server-side trailer |
| 19 | | the `0xFF` terminator |

`ActorStatsUpdateCommand` carries **current hit points as an int64, then the resource
a skill spends as a 32-bit float**, then the actor and the terminator. This was
misread twice, and each reading looked reasonable: byte 0 takes only three values
across a whole session, so it looked like a stat selector, and then the pair looked
like a maximum and a current value. The client's observer vtable settles it — the
first field's setter notifies `OnHealthPointsChanged` and the second's
`OnSkillResourceChanged`. So 234–236 was a nearly full health bar, and the float
falling by exactly 5.00 per cast and recovering at 0.2 was mana or rage. Writing
damage into the second field drains the resource and leaves health untouched.

The retail server only ever sent this for the player, but nothing in the client
restricts it — that is server policy, not a client rule. It is also unnecessary,
because `HitCommand` already carries the victim's health.

**Where a creature's health bar comes from:** `NewMonsterCommand` at spawn — field 3
is current hit points and field 4 the maximum, both int64 — and thereafter from
`HitCommand`, which carries both on every blow. Fields 1 and 2 of that command are
variable-length, so the offset of the pair has to be computed rather than assumed.

Useful asymmetry: health always reaches the actor's attribute module even with no
entity bound. Only the *visual* needs the binding.

**What kills, and what does not.** `0x0073 ActorsLeftVicinityCommand` does *not*
kill: it sets each named entity invisible. Sending it as a death leaves the creature
standing there at zero health. What kills
is **`0x006C KillCommand`** — a tick, damage types, the killer, a float3 impulse the
corpse is thrown with, a kill tick, and a single despawn bit — and the client's whole
death sequence hangs off it. `0x002B DiscardMonsterCommand` is a third thing again:
an instant deletion of entity and actor, no animation, and its body is **empty**, so
it is seven bytes on the wire and needs no recording. Sending it in the same breath as
a kill destroys the death sequence before it can play.

`HitCommand`: damage is an **int32**, the victim is in the **trailer** rather than
the body, the attacker is a separate field, and the victim's post-blow current and
maximum health travel in the same message. Five of its eighteen fields are single
bits, so it cannot be assembled from whole bytes.

Two of its fields were invented here and 74 real hits — two sessions, two creature
skills — refute both unanimously. `kind` is **3**, never 0. The floating number
belongs to the **attacker**, never the victim; an earlier note here claimed the
client draws nothing unless it is the local player's actor, and the combat value
alongside it is 0.0 in all 74.

`0x0073` and `0x0074` are the same shape as each other and byte-aligned throughout: a
32-bit count, that many 32-bit actor ids, then the trailer. Any set of actors can be
announced or hidden in one generated message.

The client also validates its own reach before sending anything:
`SkillValidator: target for 'angrystrike' out of range!! 4.17 <-> 1.75`. So a server
need not check range; if the attack arrived, it was in range. What a server *must* do
for the player's own blows is decide **which** creature was hit: choosing afresh on
every blow makes the choice flip between creatures standing close together, which
reads on screen as damage being shared out. Latch it instead.

### The swing

A creature animates only when sent `0x0047 TargetSkillCommand`, and the command is
**64 bytes**. An earlier note here called it twenty-one and said it named no target;
both were wrong, and the missing fields are why creature attacks never animated —
the terminator landed where the client expects the impact tick, so the visualizer
got nonsense for its duration and its position and finished as it started.

| field | width | value |
|---|---|---|
| high water | uint16 | 0; read but never written |
| skill | uint16 | zero-based index into `_Template_Skill` |
| heading | float32 | from the **target** back to the attacker |
| start tick | uint32 | three ticks ahead of now |
| — | uint32 | 0 |
| — | 1 bit | 0 |
| target | uint32 | actor |
| attacker | uint32 | actor |
| impact tick | uint32 | start tick + `HitFrame` |
| hit frame | uint32 | `HitFrame` |
| unblock | uint32 | `SkillUnblockFrame` |
| motion unblock | uint32 | `MotionUnblockFrame` |
| — | uint32 | 0 |
| position | 3 × float32 | the attacker's, in the description frame |
| — | float32 | 1.0 |
| — | 1 bit | 0 |
| | | the `0xFF` terminator, no trailing actor |

Every derived field is confirmed against two live attacks by different creatures:
8890 + 15 = 8905 and 8905 + 19 = 8924 for the impact tick, 30/30 and 41/41 for the
two durations, and both commands reproduce byte for byte. The heading is the reverse
vector, which fits both samples to within 0.3° where the forward vector misses by
180 — a creature sent the forward one swings away from the player.

The command rides **inside a movement batch**, behind the records; all 77 observed
did, and none travelled alone.

The rest of the blow comes from the skill's own row rather than from invention. For
`AnderworldCreatureStrike`: `HitFrame` 12 (the hit is sent twelve ticks after the
swing), `AttackRange` 2.0 (stop closer than this or the creature halts outside its
own reach and creeps in for ever), `HitRange` 2.25, `CoolDown` 2.75 s, and
`DamageType` `DarkMagic;Physical` — two entries, where the player's `angrystrike`
has one.

## The character-selection state machine

This is the part that took longest, because the two message ids involved are easy
to swap and swapping them makes the whole flow unexplainable.

* `0x0087` is `Commands::CharacterSelectionCommand`.
* `0x0086` is `Commands::CharacterGenerationCommand` — character **creation**.

The selection command's body begins with an 8-bit `operation`, and that operation
is the state machine:

| operation | direction | meaning |
|---|---|---|
| 1 | server → client | here is the character list |
| 2 | client → server | send me the character list |
| 3 | client → server | **start the game with the selected character** |
| 4 | server → client | refused, with a reason |
| 5 | server → client | **granted**; the client enters its game state |
| 7 | server → client | refresh the list |
| 9 | server → client | update one character |

Three consequences, each measured:

**The server must push operation 1 unsolicited.** The client does not ask for the
list on a fresh login. In the client, the only callers of its
`RequestCharacterList` are a post-creation refresh and a cancel button; the login
path calls none of them. Until the push arrives, the manager's state is 0, the
selection *widget is never constructed*, and there is no button to press.

**The push is refused if it arrives twice.** The handler requires the state to be
at most 1, so a second push silently does nothing.

**After clicking, the client waits for operation 5 with no timeout.** It sends
operation 3, sets its state to "start-game sent", and stays there. A server that
does not answer produces a client that looks frozen on the selection screen having
apparently never offered the button — when in fact it offered it, the user clicked,
and nothing came back. The grant must carry a **non-zero character id**, or the
client rejects it and stays put.

On the wire, in the reference session:

```
frame 102    S 0x84/0x0087  316 B  operation 1   the server pushes the list
frame 2989   C 0x8b/0x0087   32 B  operation 3   the client asks to play
frame 3633   C 0x8b/0x0087   32 B  operation 3   it asks again
frame 19147  C 0x8b/0x0086  106 B  operation 1   the player creates a character
frame 36658  S 0x84/0x0087   32 B  operation 5   the server grants
```

Note that the server sends **nothing at all** between the client's first request
and its grant — seventeen thousand frames of nothing but the client's own pings and
time syncs. That gap is a human deciding, and it is why the grant looked as though
it were triggered by something else.

## Transport

RakNet 4.x, protocol version 5. The offline handshake (`0x05`–`0x08`),
`CONNECTION_REQUEST` `0x09`, `CONNECTION_REQUEST_ACCEPTED` `0x10`,
`NEW_INCOMING_CONNECTION` `0x13`, `ID_TIMESTAMP` `0x1B`. MTU negotiates to 1292.

* **Datagram header**: byte 0 is a bitfield (`0x80` valid, `0x40` ACK, `0x20` NAK,
  `0x10` pair, `0x08` continuous, `0x04` needs B and AS), then a 24-bit
  **little-endian** sequence number.
* **Frame header**: a flags byte (`reliability << 5`, `0x10` marks a split), then a
  16-bit **big-endian** payload length **in bits** — not bytes. Then, depending on
  reliability: a 3-byte LE reliable index; a 3-byte LE ordering index plus a 1-byte
  channel; for a split, a 4-byte BE fragment count, 2-byte BE id and 4-byte BE
  index.
* **SystemAddress**: version byte 4, the IPv4 octets as their **bitwise
  complement**, then the port big-endian and uncomplemented.

Five faults at this layer each stall or kill a client in a way that looks like a
missing feature rather than a transport problem.

**A sequenced frame must not consume an ordering index.** It carries both an
ordering and a sequencing field, but the ordering one *reports* the index the next
ordered message will use; only the sequencing field is its own, from a separate
per-channel counter that resets whenever an ordered message goes out. Taking one
index for each field spends two per sequenced message and leaves a hole in the
ordered stream — and a peer cannot deliver anything past a hole. One unsolicited
time-sync announcement was enough to strand the character list and everything
behind it in the client's reorder buffer for good: the client sat on the map named
in an earlier message and the selection screen never appeared. The capture settles
it — the real server's `0x83` and the list immediately after it both carry ordering
index 4.

**Acknowledgements must not be paced.** Outbound datagrams are worth rate-limiting;
a seven-byte ACK is not. Queued behind a 717 KB transfer at 250 datagrams a second,
one arrived 594 datagrams — 2.4 seconds — after the message it acknowledged. The
client resends an unacknowledged reliable message about every 100 ms, so it sent
its ready signal twenty-three times. All twenty-three carried **reliable index
4**: one message, not twenty-three requests. Counting distinct reliable indices is
the cheapest way to tell a retransmission from a repeat.

**The rate matters, and faster is worse.** The real server sustained 250–300
datagrams per second and never more. Pushing 594 at roughly a thousand a second
lost 133 of them immediately: the peer's receive buffer is the constraint.

**Retransmission is on a timer, not only on NAK.** The real service sent 32,176
fragment datagrams to deliver 596 distinct fragments over 101.6 seconds — a
fifty-four-fold amplification, with one fragment sent 230 times — while the client
NAKed nothing at all. Waiting for a NAK means one lost NAK strands a fragment
forever. Resend oldest-first: a peer reassembling a split message is blocked on its
earliest gap, so resending a later fragment it already holds advances nothing. Only
reliable datagrams are worth retaining; resending an unreliable one defeats its
purpose.

**One message must arrive slowly.** The 717 KB event schedule crashes the client if
it lands while the selection screen is still being built. The client's own log gives
the order away: against the real server it logs the screen being built and switched
*before* `handle event updates`; against a server that delivers the schedule in 2.4
seconds it logs `handle event updates` first and then dies with an access violation
in the audio thread the screen build starts next. The real service never triggered
this because its transfer took 101.6 seconds to complete — six useful fragments a
second, buried in retransmissions — so the schedule landed long after the player had
left the screen. Reproducing that rate is not a workaround for a bug of ours; it is
reproducing a property the client depends on.

## Message shapes

Which shape applies is decided by the message id. There is no single envelope, and
assuming one is a trap — the two bytes after `0x86` look exactly like an opcode but
are the length of the string that follows.

| ids | shape |
|---|---|
| `0x84` `0x85` `0x8B` | id, 16-bit **little-endian** opcode, opaque body |
| `0x82` `0x86` `0x8A` | id, then length-prefixed strings (16-bit LE length) |
| `0x88` `0x8D` | a bare one-byte signal; its arrival is its content |

`0x1B` is RakNet's `ID_TIMESTAMP` and not a game message: it prefixes a real
message with an 8-byte clock.

`0x86`, the map assignment, writes the map name **twice**, followed by a 32-bit
trailer — `0xFFFFFFFF` for `a0000_char` and small integers for real maps. Sending
the name once is the kind of difference a client validates and rejects.

`0x84/0x0070`, the handoff, is `host:port` as a length-prefixed string plus one
trailer byte. That byte is **not** constant, and it tracks the destination:

| handoff | trailer |
|---|---|
| to the character service | `0x80` |
| to a map server | `0x00` |
| the empty one that releases a client | `0x00` |

`0x84/0x010E` is a purchase-offer list, answered to a client `0x8B/0x010B` and paired
with a small `0x84/0x010C`. Its ordering indices sit right behind the release, which
is why it was briefly mistaken for part of it; it is an answer to a request.

```
u8 0x84 / u16 0x010E / u32 count
per offer:  105 bits, then three length-prefixed strings
            the price is a 32-bit float at bit 32 of those 105
            then a product label, a payment method (usually empty), a validity date
then ~27 bits of trailer, carried verbatim
```

Both recorded samples round-trip byte for byte, and 105 is not a multiple of eight, so
the strings drift a nibble per entry and a byte-aligned search finds about half of
them. The number *in* the label is the quantity — 1500 andermants — and not the price,
which is the separate float: the recorded list prices its bundles 1.99, 4.99, 9.99,
24.99 and 49.99 there, and the client displays exactly those figures. Re-emitting the
list with one entry at a price of zero is the cheapest proof that a format is
understood rather than replayed, because the client then has to accept bytes no real
server ever sent.

## Per-tier behaviour

Reliability is per message type, not one setting for the connection:

| message | reliability |
|---|---|
| `0x00` ping, `0x03` pong | `UNRELIABLE` (one ping was `RELIABLE`) |
| `0x83` time sync | `UNRELIABLE_SEQUENCED` |
| everything else | `RELIABLE_ORDERED`, channel 0 |

**The game clock is announced, not answered.** The server sends `0x83` unprompted —
frame 99, against the client's first request at frame 110 — and the client adopts
the value: it announced 354,685,782 and the client's next reading was 354,685,819.
The value is the **emitting service's own uptime**, not a shared epoch: a map server
announced 5,573 in the same session. Echoing the client's own clock back tells it
nothing, and it then reports that it is still synchronising its world for as long
as it is left running.

How often each tier answers a client's time sync, counted per connection:

| tier | requests | answers |
|---|---|---|
| login | 1 | 0 |
| character | 24 | 1 |
| map | 20 | 17 |

That shape is consistent with what each tier is for: the selection screen has no
world whose clock needs following, and a map does.

The login server's send order also depends on the destination, and both orders
occur. Measuring this needs the client's two login connections kept **apart** —
ordering indices restart with each connection, so a survey that merges them reads
one connection's handoff against the other's signal:

| destination | order |
|---|---|
| character service | handoff, then `0x88` |
| map server | `0x88`, then handoff |

## The character service, in ordering-index order

Frame order in a capture is misleading here: several of these travel in one
datagram and a split message's fragments interleave, so the ordering index is what
to follow.

| index | message | size | notes |
|---|---|---|---|
| 0 | `0x10` | 96 | connection accepted |
| 1 | `0x82` | 24 | service identity, answering `0x13` |
| 2 | `0x86` | 31 | map assignment `a0000_char`, answering the client's `0x8A` |
| 3 | `0x88` | 1 | bare signal |
| 4 | `0x84/0x0087` | 316 | the character list, operation 1 |
| 5 | `0x84/0x001B` | 12 | |
| 6 | `0x84/0x00DD` | 717 KB | the event schedule, 596 fragments, sent slowly |

Then, on the client's `0x8B/0x0087` operation 3:

| index | message | size | notes |
|---|---|---|---|
| 7 | `0x84/0x0087` | 32 | operation 5, the grant |
| 8 | `0x84/0x0070` | 6 | the empty handoff |

The reference session also shows a 112-byte `0x84/0x0086` here, and it is *not* part
of the release: it answers the character-creation command that session's player
happened to send. A client selecting an existing character never sends one.

## The map server, in ordering-index order

| index | message | size | notes |
|---|---|---|---|
| 0 | `0x10` | 96 | |
| 1 | `0x82` | 23 | `DrasaOnlineMapServer` |
| 2 | `0x86` | 59 | the map name, twice |
| 3 | `0x88` | 1 | |
| 4 | `0x84/0x001B` | 12 | |
| 5 | `0x85/0x0114` | 1261 | a cosmetics/unlock table |
| 6 | `0x85/0x001D` | 631 KB | **the zone content**, 513 fragments |
| 7 | `0x85/0x00A7` | 40 | |
| 8 | `0x85/0x0074` | 182 | |
| 9+ | `0x85/0x004F` | 96 each | thousands of them, the per-tick state |

Index 6 is what populates the world, the player's own actor included. Skipping it
produces a client that reaches the map, finds an empty zone, and dies on the
assertion that its local player actor is valid.

## The wire index of an effect is `rowid + 15`

The client turns the 16 bits at the head of a status effect element into a template
through `StatusEffectManager::StatusEffectTableRowToId(int)`, which indexes its own
`effectInfos` array. `Load()` resizes that array to `_Template_StatusEffect`'s own row
count before filling it, so it is one entry per row and not compacted — but the array
the wire indexes begins **sixteen entries before the table's first row**.

Measured on a capture of the live service with each cast isolated by thirty seconds:

```
5184 5185 5194  skill_frenzyshout_buff_{armor,resistance,lifeleech}
5169 5542       warrior_spikedShield_buff, _armor_trigger
5165 5166 5168 5518 5519 6212   warshout's six
5158            skill_seismicslam_debuff_armor
```

Eleven correspondences, every one of them `rowid + 15`. Read with `rowid - 1` the same
capture claims Dragon Hide applies daily-challenge blessings and Spike Shield applies a
frostnova debuff. It is not a sort: sorting the table by `Id` matches 0 of 11.

**The conventions are per table.** `_Template_Skill` is `rowid - 1`, and that one is
confirmed by the *client's* traffic rather than by anything served here — 1,581 real casts
carrying 1838 for angrystrike, 1846 for frenzyshout, 1854 for spikedShield. Only the
status effect table is shifted, so `dsor/database.py` keeps a per-table offset.

`rowid - 1` survived every check for weeks because the checks ran over captures that are
for the most part **this emulator's own traffic** — half of 144 captures are. This server
had written those indices itself with `rowid - 1`, so reading them back the same way
returned the names it had put in. One check even tested 41 candidate offsets and declared
`rowid - 1` uniquely best. The error was only ever visible against traffic this server
did not write.

## What the client does with a status effect

Read out of `dro_client64.exe`, which carries every function signature as a plain string.
The handler at `+0x34cca5`:

```
type check                        -> "called with command of wrong type"
[command+0x18] != invalid actor   -> "InvalidActorId for effect host actor"
ClientActorManager::Instance()
look the actor up by id           -> "unknown actor (id: %u | effects: %s)"
zero elements                     -> "Received empty StatusEffectCommand!"
per element, create an instance
instance null                     -> next element, AND NOTHING LOGGED
```

That last line is why the client's own logs are silent while nothing appears on screen.

Creation is gated on a clock comparison at `+0x34cf54` — create only if
`|now - end| <= 5` ticks, or `now < end`, or `start == end`, where 5.0 is a float in
`.rdata` and *now* is the client's clock, not the server's. And the creation function in
`statuseffectmanager.cc` asserts `causer.isvalid()`, `effectTemplate->IsValid()`,
`0 < causerLevel` and `MaxActorLevel >= causerLevel`. The third of those is what sent this
server looking for `0x007C PlayerLevelUpdateCommand`, which it had never sent.

The fourth element flag, at `[element+0x24]`, is tested before the parameters are read:
clear, and the element is skipped.

`tools/frida/hook-dso.ps1 -Effets` installs three hooks on that path — the handler, the
row-to-id resolution and the instance creation — so what the client makes of a message can
be read from inside it rather than inferred.

## The character list record

The list is written with RakNet's `BitStream`, which packs a boolean into one bit,
so **nothing in it is byte-aligned**. A 437-byte record held two plain-text
character names and no byte-aligned search found either: the first begins at bit
225, the second at bit 1323.

Getting the bit numbering's sign wrong is what delayed finding them. A buffer
shifted left by *b* bits puts byte *i* of the shifted copy at absolute bit
`8*i + b` of the original — not `8*i - b`.

Both known samples parse to the last bit. Offsets are into the body, after the
three header bytes.

```
bit   0   u16 LE   operation (1 = list push) and deny reason
bit  16   u32 LE   a character id
bit  48   u32 LE   the account id
bit  80   u32 LE   4        constant across two unrelated accounts
bit 112   u32 LE   0        constant
bit 144   u16 LE   1        constant
bit 160   u16 LE   10531    constant
bit 176   1 bit    0        constant
bit 177   u32 LE   how many characters follow
bit 209   the first character record
```

Each character is a length-prefixed name, a length-prefixed last-map name, then an
834-bit block:

| block offset | width | notes |
|---|---|---|
| +0 | u32 | unexplained; 0, 1, 2 and 3 seen, one per entry |
| +64 | u32 | still unaccounted for |
| +96 | u32 | the character id |
| +160 | u32 | **the account's andermant.** 4,814 in all four characters of one live account and 600 in the recording — which is what the operator saw on the live service and on this server. It was read here as "the class" on the strength of one value per account; being equal for two mages of different levels is equally consistent with a currency, and a currency is what it is |
| +256 | u32 | **the experience.** 882,246,499 / 882,260,621 / 882,269,671 / 128,322 across four live characters, 0 in the recording |
| +288 | u32 | **the level.** 100, 100, 100, 18, and 1 in the recording. 128,322 experience reads level 18 through this server's own curve as well, and 882 million reads 104 where the roster says 100 — so the level is *stored*, and the curve is wrong above 100 |
| +320 | 512 bits | a 4×4 float matrix, byte-identical across every sample: a 180° yaw with no translation |

The entries are back to back and their strings are inline, so the fields are counted
from **the end of the entry's map string** and nothing else works: the live service's
level sits at bit 1274 and the recording's at 1314, exactly the 40 bits by which the two
map names differ. From the end of one map to the next name's length prefix is 834 bits,
constant across four entries.

The same two numbers appear again in the 631 KB player state, which is what the *game*
reads while the roster feeds the selection screen — level and experience 210 and 242
bits past that message's own map string. Writing one and not the other is why the level
was right on the screen and back to 1 after pressing Play.

After the last character comes one global equipment list: a 32-bit count, then per
entry a length-prefixed template name, 20 zero bytes and one set bit. There is
exactly **one** such list even when two characters exist, and entries carry no owner
— so the record cannot bind gear to a character except by position.

## The inventory

`0x0054 InventoryInfoCommand`, and the item records inside it. Read out of the client's
own decoder rather than fitted to a capture: `Commands::InventoryInfoCommand` registers
its `Rtti` at `+0xabefc` with the fourcc `'IvIC'`, its creator reaches a constructor that
plants the vtable at `0x14115fcd8`, and slot 5 of that vtable — the slot
`DrasaClientHandler::DecodeCommand` calls for a command's own fields — is a flat run of
reader calls, one per member.

| member | shape | what it is |
|---|---|---|
| `+0x20` | array of `ItemInfo` | the records being merged |
| `+0x30` | dict of id → array of int8 | per storage, its shape |
| `+0x58` | dict of id → u32 | **item → cell in the bag.** 152 entries live, cells up to 161 |
| `+0x80` | dict of id → u32 | empty in every sample |
| `+0xa8` | dict of id → u32 | **item → the slot it is worn in.** Exactly fourteen entries in both live replies, values 0–14, against a character's fourteen equipment slots |
| `+0xd0` | dict of id → u32 | empty in every sample |
| `+0xf8`, `+0x118`, `+0x138` | arrays of u32 | ids; `+0x138` holds the cell a new item goes to |
| `+0x158` | array of id pairs | empty in the recorded reply, one pair in one live reply, none in the other |
| `+0x180` | array of u32 | 60-odd consecutive ids |
| `+0x1a0` | array of strings | 247 template names in one, quest keys in the other |
| `+0x1c0` … `+0x1d4` | 6 × u32 | unaccounted for |
| `+0x1d8` | float | **the player's current health** |
| `+0x1dc` | float | **the player's current resource** |
| `+0x1e0` | 1 bit | always set |

Every count is 32 bits and the client refuses one above 1,000,000 (`cmp ecx, 0xf4240`),
so this server holds itself to the same ceiling.

An `ItemInfo` is variable-length — four strings and two counted arrays inside it — which
is why no fixed offset into this message means anything. Three of its fields are named:
the blueprint, the position it lay at while on the ground, and six trailing `u32` that
are a **date**: the gem picked up during the live capture carries 2026‑09‑01 22:34:08 and
the ammunition the character already had carries 2025‑10‑12 11:40:51.

The proof that the transcription is right is that the walk lands, to the bit, on the
32-bit actor and the `0xFF` that end every command: the live service's two pickup replies
are 126,808 and 127,768 bits long and the walk ends at 126,768 and 127,728. A layout one
bit out does not arrive there — it dies inside a string with an absurd length, which is
what every earlier reading of this message did.

**What not knowing it cost.** The reply to a pickup used to be replayed with its fields
found by searching for the recorded item's actor and by arithmetic on the command header,
and the operator reported the two consequences for weeks:

* *"dans l'inventaire j'ai pas le bon item"* — the item record was replayed whole and
  only its name was ever rewritten, and only when a blueprint was configured. So the
  ground showed the drop recording's mace and the bag showed the pickup recording's
  sword.
* *"ça me baisse ma vie a 200"* — `+0x1d8` is the player's current health, and the reply
  was recorded from a **level 1** character. Every pickup told the client the player had
  236 health. That the scalar is the health is measured, not inferred: the second live
  reply carries 2,634,612.5 there and the `ActorStatsUpdateCommand` messages either side
  of it carry 2,616,196 and 2,639,085 for the same actor.

Both are gone because the command is now decoded, edited and re-encoded rather than
spliced. The splice also wrote its own array at bit 1,412 — which is where `+0x58`
begins, 352 bits before the allocations it meant — and it happened to be well-formed
there, which is why it was never caught by anything the client said.

## Mounts, and using an item

There is no mount command. The operator rode a manticore during a capture and it came
out as three messages:

```
client   0x0135   1b 00 "mythical_mount_manticore_01"
server   0x004F   cast_mount_manticore_01,   87 ticks
server   0x004F   ride_mount_manticore_01,  900,000 ticks
```

The rest is a column: `_Template_Item.StatusEffectId` names the effect an item applies.
2,862 of the client's 15,645 items carry one and 265 of those are mounts, so nothing has
to be inferred from the item's name. The ride is that effect's name with `cast` swapped
for `ride`, and its 900,000 ticks are 36,000 seconds — ten hours, which is how the game
says "until you get off".

Two details that would have been wrong if guessed. The duration comes from the **item**,
not the effect: the manticore item says 3.5 seconds and the effect table says 6.0, and
the service sent 87 ticks — 3.48 seconds. And the command's class in the client's Rtti is
`UseStickerBookItemCommand`, which is not what it does; its body is a length-prefixed
item template name and nothing else.

Everything after that is the status effect path that was already there, which is why this
was a small change. It also means the other 2,597 usable items work by the same route.

## What a skill's effect column says

The format is `name,C:1.0,D:8.0,DP:8.0,DE:1.0,TR:1.0,$0:-0.4,On:Hit;name,...`, and for a
long time this server read four of those fields and dropped the rest. The dropped ones
are the common ones: `DP` appears 2,741 times across the client's skill table and `TR`
550, against `DE`'s 345.

| field | uses | what it is |
|---|---|---|
| `C` | 3,500 | the chance on the cast. 1.0 is applied, 0.0 needs something else |
| `DP` | 2,741 | a duration — see below |
| `D` | 2,731 | the duration, overriding the effect's own row |
| `$0`…`$4` | 2,010 | the element's parameters. A `$n` naming a variable rather than a number contributes zero, which is what a character with no talent to set it has |
| `ON` / `On` | 693 | the event that applies it instead of the cast |
| `TR` | 550 | seconds between ticks. 377 entries say 1.0 |
| `DE` | 345 | seconds before it starts |
| `OZ` / `OX` / `OY` | 308 | an offset, on ground effects. Still unread |
| `OFF` | 27 | the event that ends it |

**`DP` against `D`.** They are equal in 2,504 of the 2,697 entries carrying both, and
where they differ `DP` is smaller and the effect is crowd control: `debuff_cc_stun` is
`D:5.0,DP:1.5` and `debuff_cc_petrify` is `D:5.0,DP:3.0`. That reads either as a short
effect inside a long immunity or as one shortened in player-versus-player, and this
project has no way to tell yet.

What it *can* tell is what to do when there is no `D` at all, which is 24 entries.
warshout's `ctfdropflag` is one, at `DP:10.0`, and its own row in the effect table says
0.0 seconds — so reading only `D` gave it no duration and it was never sent. The README
used to explain that as the service not applying it, measured over "42 casts, four
effects each time, never that one". **Those 42 casts were this server's own traffic.**
The live service applies it: fourteen elements in `officiel4`, every one at 250 ticks,
in the same message as the buffs beside it.

The same correction lands on `skill_warshout_buff_mightybash`, whose column reads
`C:1.0,DP:10.0,DP:10.0` — `DP` twice and no `D`. It was written down here as lasting one
second, which is the effect table's figure and what this server produced. The service
sends 250 ticks, fourteen times out of fourteen.

**The `On:` triggers are read and deliberately not raised.** The vocabulary is eleven
events and closed: `skillstart` (384), `kill` (141), `hit` (106), `hitmarked` (23),
`hitcritical` (13), `hitfrost` (8), `lock` (5), `summonsdead` (5), `hitmagecharged` (4),
`hithostile` (2), `hitally` (2). Both the key and the value vary in case — `ON:SkillStart`
592 times and `On:skillstart` 101 — so a case-sensitive parser reads one in seven.

Raising them would be a mistake, and the measurement says so rather than an opinion:
**638 of the 693 name an effect belonging to a piece of gear, an item set, ammunition, a
rune, food, a skill book or a talent**, none of which this server models. Of the 55 that
remain, all but a handful are boss and monster skills. For the warrior's ten skills the
number of trigger entries that a character with no gear and no talents should receive is
**zero** — `earthquake` alone names 25 `ammunition_damage_*_activate_minion_*` entries at
`On:SkillStart`. Firing them would not add what the game does, it would add another
character's equipment.

Which is also why "26 of the 82 skills place no effects", written here earlier, was an
artefact of the query and not a fact about the game: `granted_by` keeps the `C:1.0`
entries, and everything else in those columns is waiting on an event or on gear.

## What an item is, and how the stats work

The three things the operator kept asking about — the `On:` triggers that cannot be
raised, the stats that never change, the inventory that does not persist — all wait on
the same missing piece, and the client ships it whole in two tables.

`_Template_Item` says what an item **is**. The torso picked up during `officiel4`:

```
SlotType          TorsoSlot
CharClass         warrior
ItemCategory      Armor
BaseEnchantments  item_base_armor_torso;item_base_block_torso;
                  item_base_speed_movement_torso
MinDropLevel 1    MaxDropLevel 200    RequiredLevel 1
```

`_Template_Enchantment` says what a statistic **does**, and its `Modifiers` column is
the whole vocabulary of a character's numbers:

```
<Attribute>:<value>[|<max>],<absolute|relative>[&<condition>][,<qualifier>...]
```

Settled across all 4,108 rows: 2,562 modifiers carry an attribute and a mode, 1,270 add
one qualifier, 270 add five (the damage types), 6 add two. Eight hang a condition off the
mode with an ampersand — `relative&emperorgold`, `relative&rarityunique` — rare enough
for a spot check to miss and enough to break a parser that compares the field to
`relative`.

**There are 24 attributes in the whole game.** ItemDamage (969 uses), ItemResistance
(755), ItemArmor (494), MaxHealthPoints (349), ItemCritical (292), ItemSpeed (290),
ItemCriticalValue (235), ItemHealthPoints (178), ItemBlockValue (158), Resistance (155),
ItemRiftForce (100), MaxSkillResource (34), Block (22), XPGain (14), Speed (13), Critical
(12), DropAmount (10), SkillResourceRegeneration (6), HealthPointsRegeneration (5),
CriticalValue (5) and four rarer. So a character's stats are the sum of their items'
enchantments in those names, and that is the answer to how the stats work.

**What the wire carries.** An item record names its rolled enchantments and gives each a
value between 0 and 1 — the roll, not the result. The torso carried `item_block_torso` at
0.8079 and `item_armor_torso` at 0.8210. The base enchantments its template names are
**not** in the record; they are implied. So reading an item takes both tables, and the
record alone is not enough. Two record fields also got names from it: `+0x0c` is the item
**level**, 125 on that torso and the same 125 in each of its statistics, and `+0x38` is a
**tier**, 5 there and again 5 in each statistic.

**Where this stops, on purpose.** A modifier that states a range interpolates:
`ItemArmor:0.599|1.498,absolute` at a roll of 0.8210 is 1.33708 per level of the item,
and the base torso enchantments all read that way. A modifier that states one value —
`ItemArmor:0.204,relative` — does not say what the roll does to it, and there are two
readings, so asking for it raises instead of picking one. The tier's meaning is likewise
unproven, so a dropped item's tier is zero rather than a made-up curve.

**What it already changes.** A kill used to leave the recording's mace, every kill,
forever. It now leaves a real item of the player's own class and level, with statistics
its template can actually carry — `item_speed_attack_shoulders` on shoulders,
`item_resistance_fire_gloves` on gloves — and the pickup reply describes the same item the
drop named. Seeded by the drop count, so the same kill leaves the same thing twice.

## The event schedule

`0x84/0x00DD`, the largest message in the protocol at 733,774 bytes, is neither
inventory nor character state. It is a flat list of 8,233 dated event-schedule
entries, identical for every player: PvP match cycles, seasonal events, shop
promotions, difficulty unlocks.

```
u8  0x84 / u16 LE 0x00DD / u32 LE count
per entry:
  u32 LE  id            strictly increasing, unique
  u16 LE + bytes        the event key
  1 bit                 set in 16 of 8233
  3 bits                always 0
  6 x u32 LE            always 0
  6 x u32 LE            year, month, day, hour, minute, second
  u32 LE  n             parameter count
  n x (u16 LE + bytes)  parameters
4 bits of zero padding
```

The layout is verified by round-trip, not argued: decoding the recorded message and
re-encoding it reproduces all 733,774 bytes, accounting for 5,870,188 of the file's
5,870,192 bits. The other four are padding to the byte, which is why frames carry a
length in bits.

Two properties are worth knowing. The entries are ordered by id and **not** by date
— the dates go backwards 501 times. And the gap from one entry's text to the next
entry's length prefix is a constant 452 bits, which is not a multiple of eight, so
every second string starts on a nibble boundary. That is why a byte-aligned search
finds only half of them.

## Refuted

Kept because a plausible idea that was checked and failed is worth more than the
same idea rediscovered later.

* `0x8B/0x0086` is **not** "enter world". It is character creation. Believing
  otherwise made the Play click look like a message the real service ignored, so
  this server ignored it too, and the client waited for a grant that never came.
  This one cost more than every other mistake here combined.
* The 717 KB transfer holds **no** account state, inventory or appearance. Every
  string in it is an event or promotion key.
* The character list record holds **no** appearance data either — no hair, face,
  body or gender field survives a full parse of both samples.
* Item entries in the record carry **no** slot, owner, level or enchantment: 20 zero
  bytes and one true boolean.
* The record's header is **209** bits, not 225. Bit 225 is where the first name's
  *text* starts, because the 16-bit length prefix occupies 209–225.
* The server does **not** echo a client's position bit-identically. Two hand-picked
  samples said it did; measured across 513, only 106 matched.
* Bytes 7–8 of a movement message are **not** a duplicated heading. They agree
  about half the time, which is what noise looks like.
* `0x85/0x0114` does **not** place the character, despite its size and its position
  in the sequence. A bit-level scan finds nothing in it but `skin_unlock_amount_01`
  through `09`.
* The confirmed character id does **not** appear in the entity snapshot — not in
  any of three coherent sessions. Entities are not addressed by it.
* A list the client seems to ignore is **not** an account mismatch: the client's own
  `-accid` is present in the record, at bit 48.
* `char_gen` audio events and an FMOD worker thread are **not** signs of the client
  falling back to character creation. They appear in the real session too — the
  client builds that model to display it.
* `5F 00` is **not** a separator between chained entity records. It is the next
  command's id, and `0xFF` is the terminator of the one before. The wrong reading
  emits byte-identical output, so nothing on the wire can catch it.
* Byte 0 of a stats update is **not** a stat selector, and byte 15 of a movement
  record is **not** an entity index. They are the low byte of a 64-bit maximum and
  the low byte of a 32-bit actor id.
* `0x002A` does **not** merely accompany a creature's arrival, and it is **not** a
  zone trigger despite carrying names like `..._creature_1st_encounter`. It is what
  creates the creature.
* A creature's position **cannot** be changed by a movement update, and a creature's
  health cannot be reported at all. Both are discarded for want of an actor→entity
  binding, silently, with nothing in the client's log.
* Rewriting the position inside a description at a fixed byte offset **breaks it**.
  The message is bit-packed and ends three bits short of its last byte; reading at an
  approximate offset forgives, writing does not — the creatures stopped appearing.
* Filtering attacks by the client's own 1.75-unit reach refuses **every** blow, and so
  does 6. See the two coordinate frames above.
* The status effect table's wire index is **not** `rowid - 1`. It is `rowid + 15`, and
  the wrong value was confirmed repeatedly against captures that are mostly this
  server's own output, where these very indices had been written with `rowid - 1`.
  Sending 5168 and 5169 for Dragon Hide made the client show "Power of Smash" and
  "Spike Shield", which are the rows sixteen earlier — reported accurately, and for
  weeks, before it was believed.
* Bit 168 of the roster is **not** the level. It reads 1 in the recording, which is a
  level-1 character, and 1 in the live service's level-100 characters as well. Writing
  104 into it broke the login.
* The andermant is **not** in the player state. A single 600 found there matched the
  amount on screen and writing it changed nothing; the amount is in the roster, at
  `map_end + 160`, and it is the account's rather than the character's.
* The client's clock must **not** be taken forward-only. It starts again near zero on a
  new session, so a monotonic rule kept the previous session's value — 35,278 against
  the client's 10,000 — and scheduled every effect a thousand seconds ahead.
* This server's tick was **not** 5,561 ticks behind the client's. That figure came from
  one capture holding two sessions, comparing the first's low tick with the second's
  high one. Element by element at the same moment it was never more than about thirty
  out.
* Filling the action bar's seventeen slots **kills the client**:
  `Util::FixedArray<Core::Ptr<UI::Slot>>::operator[]`. The declared count is seventeen
  and the UI's array is not. What can be written back safely is what the client itself
  reported.
* Throttling the event schedule to the real service's measured six fragments a second
  fixes the crash it causes and replaces it with a worse symptom: everything behind it
  in the ordered stream waits for it, so the client sits on "loading data" for ninety
  eight seconds. The real service did exactly that and its player waited forty-nine
  seconds after clicking. Delay the start instead.
* `0x007B ActorStatsUpdateCommand` is **not** `(attribute id, zero, value)`. It is the
  current health and the current resource, which `dsor/gameplay.py` already had from the
  client's own setters — the first eight bytes go to `SetHealthPoints`, the four after
  them to `SetSkillResource` — and the live service says it out loud: over one session
  its 248 messages for the player's actor hold a first field that plateaus at 2,757,733
  and falls under fire, and a float that starts at 115.6348, drops as skills are cast,
  touches 0.0000 once, and climbs back to 115.6348 every time. 2,757,733 was written down
  here as "one attribute id observed" and is the character's full health.
* The reply to a pickup does **not** need its own allocation array. The recorded reply
  carries none, and of the live service's two pickups one names a cell and the other
  names nothing at all — both put the item in the bag. The assertion that provoked the
  belief (`InvalidIndex != outItemWithLocation.primarySlotIdx`) was answered by the
  dictionary at `+0x58`, which is a different field 352 bits earlier.
* "26 of the 82 skills place no effects" was an artefact of the query, not a fact about
  the game — see "What a skill's effect column says".
* warshout's `ctfdropflag` is **not** an effect the live service declines to apply. It
  was never applied *here*, because its duration is in the column's `DP` field and the
  parser dropped it. The service sends it for 250 ticks. The 42 casts that supported the
  old claim were this server's own traffic.
* `skill_warshout_buff_mightybash` does not last one second. That figure is the effect
  table's own and what this server produced; its column says `DP:10.0` twice and no `D`,
  and the service sends ten seconds.

## Open

* **Collision and pathing.** Creatures walk straight at the player and through
  walls. Nothing here reads the map's navigation data.
* **A character's own equipment.** Items are read and generated now — see "What an item
  is" — but what the player *wears* still comes from the recorded player state, so the
  fourteen equipment slots hold another character's gear and no stat this server computes
  reaches the client. The next step is the `+0xa8` dictionary: fill it with items this
  server made, and the triggers, the stats and the saved inventory all unlock together.
* **How a rolled statistic becomes a number.** The range case is resolved; the
  single-value case and the tier are not, and neither is how many statistics an item of a
  given rarity may carry — a dropped item gets two, which is what the live torso had.
* **Character appearance.** The selection screen draws a character with neither
  hair nor equipment. Neither the roster nor the event schedule carries
  appearance data.
* **Stats other than health and resource.** `0x007B ActorStatsUpdateCommand` carries
  those two and nothing else — see Refuted, where the "attribute id" reading of its
  first field is retired. Everything else a character has (armour, resistances, the
  critical rate, the movement speed the operator watched change) has to travel
  somewhere else, and no capture has been searched for it yet.
* **What a character owns**, which is what the `On:` triggers turned out to depend on.
  They are parsed now and not raised, because 638 of the 693 belong to gear, an item set,
  ammunition, a rune, food, a skill book or a talent. So the gap is not the events: it is
  that this server has no model of equipment, sets or talents to gate them with. Until it
  does, raising them adds another character's kit.
* **`OZ`, `OX` and `OY`**, 308 uses, on ground effects — `earthquake`'s entries all carry
  `OZ:-2.5`. Almost certainly the offset from the caster at which the effect is drawn,
  which is the one thing the ground-effect command does not carry.
* **The creatures of a real dungeon.** `pw001_01_grimmagstone_01_dun` spawned
  `pw001_01_normal_skeleton_warrior_heroic` (40 descriptions),
  `pw001_03_normal_minispider_heroic` (12), `pw001_01_normal_skeleton_archer_heroic` (3)
  and `pw001_01_normal_undead_mage_heroic` (1). Their descriptions are in the capture;
  this server still serves the `a0001` tutorial creatures.
* **Talents**, and with them a skill's upgraded behaviour. `0x011A`, `0x011B` and
  `0x011D` are named and appear in no capture, so the state travels inside the player
  initialisation. `_Template_SkillTalent` has the data: 156 rows with a
  `SkillTemplateId` and the effects each grants.
* **Equipment and inventory, saved.** The command that carries them is read and written
  now — see "The inventory" — but nothing persists: the bag is whatever the recorded
  reply holds plus what was picked up this session, and it resets when the server does.
  The live service's roster carries 29 item records after the four character entries and
  the player state carries 368 distinct item ids; both are variable-length, which is why
  the fixed-width level, experience and andermant were written first.
* **Ground Breaker's shape.** It stuns on this server and the operator reports an area
  effect where there should be none.
* **The chat service on 2191**, which carries only a handshake and one channel
  identifier in every capture so far.

## What tests found what

* **Diffing this server's capture against a real one, per connection and in
  ordering-index order.** Frame order misleads; merged connections mislead worse.
  Both mistakes were made here.
* **Replaying the recorded client offline.** The capture holds every datagram the
  real client sent, so the whole flow runs in-process with no game. It reaches
  branches the live client refuses to reach.
* **The database and the binary before the wire.** Six combat numbers were
  invented here and every one had a visible symptom; all six sit in one row of
  `_Template_Skill`.
* **Measuring against traffic this server did not write.** Half of the 144 captures
  are this emulator's own output. Every measurement taken across all of them measured
  this server's mistakes back as ground truth, and the wire index survived weeks of
  checks that way. A capture of the live service made *for* one question — each cast
  isolated, the login included — settled in an afternoon what a month of inference had
  not.
* **A value the operator can read off the screen.** 600 andermant, 4,814 andermant, a
  character's name, a level: given one, a field can be found *by its value* in a
  bit-packed message. Without one, the method is to spot a plausible pattern and
  conclude, which broke the login twice. The 4,814 was already sitting in a dump taken
  hours earlier and went past unremarked because nothing had said what to look for.
* **Comparing whole messages, not prefixes.** A check here once reported a command
  reproduced "byte for byte" after comparing 26 bytes chosen by hand. The command
  is 64 bytes, and the eight missing fields were why creature attacks never
  animated. Compare lengths first.

## Layout

```
server.py               the three tiers
raknet/                 datagrams, frames, reliability, the bit-level stream
dsor/                   message shapes, the character record, the event schedule
dsor/database.py        the client's own static.db4, in memory, with the per-table
                        wire-index offsets
dsor/effects.py         the status effect and skill tables, all five classes
dsor/statuseffect.py    the 0x004F codec: every real message re-encodes byte for byte
dsor/measured.py        generated: what the live service put in an element, per effect
dsor/chain.py           walking the commands chained inside one payload
dsor/charlist.py        the roster's level, experience and andermant
dsor/playerstate.py     the same level and experience where the game reads them
dsor/actionbar.py       reading the action bar, and writing back what the client sent
dsor/vitals.py          PlayerLevelUpdateCommand, which the effect path needs
dsor/inventory.py       the 0x0054 codec: both live replies re-encode byte for byte
dsor/usable.py          using an item by name, which is how a mount is summoned
dsor/equipment.py       what an item is, and the 24 attributes stats are made of
dsor/data/              messages still replayed rather than generated
tools/command_ids.py    command ids, recovered from the client binary by name
tools/session_report.py what happened in a capture: casts, effects, creatures, loot
docs/commands.md        all 365 of them, by namespace
tests/                  the suite, including two offline replay harnesses
tools/frida/            client-side capture: the agent, its driver, the launcher
docs/logs/             redacted logs of a working session, and of a real one
```

Nothing here is affiliated with or endorsed by the game's publisher.
