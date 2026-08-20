# experimental

Notes on the Drakensang Online network protocol, written while building a server
the retail client will talk to. Everything here was measured against captures of
real sessions and against the client's own reaction; where something is a guess it
says so, and where a plausible idea turned out to be wrong it is recorded as
refuted rather than deleted.

As of the last session the client completes the whole chain: it logs in, is
dispatched to the character service, is shown its character, is granted the game,
returns to the login server, is dispatched to a map server, and enters the world at
the spawn point. Creatures appear around it, can be struck, lose health and drop
loot, and the shop shows whatever this server decides at whatever price it decides.

What it does *not* do is render that character with its hair and equipment, or let a
creature move. Both are recorded under **Open**, with what is known about why.

Two sources feed these notes: packet captures of real and emulated sessions, and
the client's own log. The client is a native Windows x64 build (no IL2CPP), so
nothing here comes from decompiled game code except a static reading of symbol
strings the binary retains in its assertion messages.

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

This matters because it is easy to misread. A multi-entity position update looks
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

## A creature has two positions, in two frames

This one cost several rounds of a human staring at a screen, and it is worth stating
loudly.

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

`ActorStatsUpdateCommand` carries **two numbers, not a selector and a value**: an
int64 maximum then a 32-bit float current, then the actor and the terminator. Three
values in byte 0 across a whole session made it look like a stat id; they were
maxima of 234, 235 and 236 — a character's ceiling drifting as it levels.

**A creature's health is never reported.** Two sessions, 154 stats updates between
them, every one for the player — including the session in which six creatures were
killed. There is no message for it, so the client derives a creature's bar from
`NewMonsterCommand` and from the blows that land.

What a kill looks like, from the session that contains six of them: a large
`0x006B HitCommand` naming both the creature and the player, then a
`0x0073 ActorsLeftVicinityCommand` naming the creature. Frames 5219 then 5410 for the
first, 5525 then 5733 for the next, and so on. The small 160-byte hits name no
creature at all — those are blows that did not land, or blows taken.

The client also validates its own reach before sending anything:
`SkillValidator: target for 'angrystrike' out of range!! 4.17 <-> 1.75`. So a server
need not check range; if the attack arrived, it was in range. What a server *must* do
is decide **which** creature was hit, because `TargetSkillCommand` names no target —
twenty-one bytes holding a skill id, a float, a tick and two unestablished fields,
and not one actor id among them. Choosing afresh on every blow makes the choice flip
between creatures standing close together, which reads on screen as damage being
shared out; latch it instead.

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
| +0 | u32 | unexplained; 0, 1 and 2 seen |
| +64 | u32 | the only per-character number not accounted for; candidate for a model or appearance parameter |
| +96 | u32 | the character id |
| +160 | u32 | **the class** — 600 warrior, 719 mage, and equal for two mages of different levels |
| +256 | u32 | experience candidate |
| +288 | u32 | level candidate |
| +320 | 512 bits | a 4×4 float matrix, byte-identical across every sample: a 180° yaw with no translation |

After the last character comes one global equipment list: a 32-bit count, then per
entry a length-prefixed template name, 20 zero bytes and one set bit. There is
exactly **one** such list even when two characters exist, and entries carry no owner
— so the record cannot bind gear to a character except by position.

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
* Throttling the event schedule to the real service's measured six fragments a second
  fixes the crash it causes and replaces it with a worse symptom: everything behind it
  in the ordered stream waits for it, so the client sits on "loading data" for ninety
  eight seconds. The real service did exactly that and its player waited forty-nine
  seconds after clicking. Delay the start instead.

## Open

* **Creature movement, health and death.** All three are per-actor state changes, and
  all three are discarded unless the client has bound an entity to that actor in
  `ClientActorManager::actorEntities`. Creation is the one thing that does not need
  the binding, which is why creatures appear and then never change. The only live
  writer of that table is `Properties::ActorProperty::OnActivate`, which depends on
  the creature's entity template — level data, not a message. A real server does
  manage it with bytes indistinguishable from ours on `0x005F`, so something is still
  missing rather than impossible.
* **`HitCommand`'s field layout.** Combat currently replays recorded hits, which
  carry another session's damage numbers and animation, and that is the direct cause
  of every incoherent state: creatures dying in one blow, standing at zero health,
  flickering. Sixty-eight bytes of a single command is a tractable decode and it is
  the clean fix.
* Why the selection screen renders a character with neither hair nor equipment.
  Neither the roster nor the event schedule holds appearance data.
* The chat service on 2191, which carries only a handshake and one channel identifier
  in every capture so far.

## Method

Three things did more for progress than any single protocol insight.

**Record your own traffic in the same format as the reference capture.** Diagnosing
a client that reaches a screen and stops had cost several rounds of guess, restart,
retest — one hypothesis per round trip, with a human in the loop each time. A
recording that can be diffed against a real session at message level tests every
hypothesis at once, and found two transport faults in its first two comparisons.

**Replay the recorded client offline.** The capture holds every datagram the real
client sent, so the whole flow can be driven into the server in-process with no game
running. That is the only way to exercise a branch the live client refuses to reach,
and a contiguity check over the resulting ordered stream catches the class of fault
that is otherwise invisible.

**Compare in ordering-index order, per connection.** Frame order misleads, merged
connections mislead worse. Both mistakes were made here and both produced confident
wrong conclusions.

**Read the client's binary before guessing.** Every stall in this project was broken
by it and none by inference from the wire: the two inverted command ids, the request
that has to be answered before a creature exists, the real field layout of a movement
record, the two numbers in a stats update. Between those, hours went into changing the
server and asking a person to describe their screen — which is slow, tests one
hypothesis at a time, and only answers the question you thought to ask. The binary
answers the question you did not.

**Read the client's log.** Every failure on the creature path writes a line there and
none of them crashes, so silence in the log is itself information. It reported its own
refusal to swing at something 4.17 units away when the skill reaches 1.75, and that
one line explained a symptom two rounds of guessing had not.

## Layout

```
server.py               the three tiers
raknet/                 datagrams, frames, reliability, the bit-level stream
dsor/                   message shapes, the character record, the event schedule
dsor/data/              messages still replayed rather than generated
tests/                  243 tests, including two offline replay harnesses
tools/frida/            client-side capture: the agent, its driver, the launcher
docs/logs/             redacted logs of a working session, and of a real one
```

Nothing here is affiliated with or endorsed by the game's publisher.
