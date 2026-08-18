# experimental

Notes on the Drakensang Online network protocol, written while building a server
the retail client will talk to. Everything here was measured against captures of
real sessions and against the client's own reaction; where something is a guess it
says so, and where a plausible idea turned out to be wrong it is recorded as
refuted rather than deleted.

The client is a native Windows x64 build (no IL2CPP), so nothing here comes from
decompiled game code — only from the wire and from the client's own log output.

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

The character service does not serve a playable map. It hands the client back with
an **empty** handoff, and the client returns to the login server for a real
destination.

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

Four things about this layer cost real time to find, and each one stalls a client
in a way that looks like a missing feature rather than a transport fault.

**A sequenced frame must not consume an ordering index.** It carries both an
ordering and a sequencing field, but the ordering one *reports* the index the next
ordered message will use; only the sequencing field is its own, from a separate
per-channel counter that resets whenever an ordered message goes out. Taking one
index for each field spends two per sequenced message and leaves a hole in the
ordered stream — and a peer cannot deliver anything past a hole. One unsolicited
time-sync announcement was enough to strand the character roster and everything
behind it in the client's reorder buffer for good: the client sat on the map named
in an earlier message and the selection screen never appeared. The capture settles
it — the real server's `0x83` and the roster immediately after it both carry
ordering index 4.

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
| 4 | `0x84/0x0087` | 316 | the roster — what the selection screen is built from |
| 5 | `0x84/0x001B` | 12 | |
| 6 | `0x84/0x00DD` | 717 KB | account data, 596 fragments |

The 717 KB transfer starts **immediately after the roster**, at frame 103, and not
in response to anything the player does. It does not have to finish first: the
client pressed Play at frame 19147 with 534 of 596 fragments delivered, and the
transfer only completed at frame 36658. Withholding it until the client asks to
enter the world deadlocks — the client will not arm its Play button until the
transfer is under way, so the request that would release it never comes.

Then, on the client's `0x8B/0x0086`:

| index | message | size |
|---|---|---|
| 7 | `0x84/0x0087` | 32 |
| 8 | `0x84/0x0070` | 6 |
| 9 | `0x84/0x0086` | 112 |

Ordering indices 10–13 are two `0x84/0x010E` + `0x84/0x010C` pairs, and they are
**answers**, not part of the release: the client asks twice with `0x8B/0x010B` at
frame 36660 and they follow at 36663 and 36666. The second `0x010E` is larger than
the first, so the two are not interchangeable. Ordering index says what order
messages are delivered in, never what prompted them.

The server **never** answers `0x8B/0x006F`, anywhere. And between the client's
`0x006F` at frame 1936 and its `0x0086` at frame 19147 the server sends nothing at
all — seventeen thousand frames of nothing but the client's own pings and time
syncs. That gap is a human deciding, not a message exchange.

## The character record

The roster is written with RakNet's `BitStream`, which packs a boolean into one bit,
so **nothing in it is byte-aligned**. A 437-byte record held two plain-text
character names and no byte-aligned search found either: the first begins at bit
225, the second at bit 1323.

Getting the bit numbering's sign wrong is what delayed finding them. A buffer
shifted left by *b* bits puts byte *i* of the shifted copy at absolute bit
`8*i + b` of the original — not `8*i - b`.

Strings use the same convention as everywhere else in the protocol, a 16-bit
little-endian length then that many bytes, just written at an arbitrary bit offset.
Confirmed: the 16 bits before an eleven-character name read as 11. Each character
is followed by the map it was last on, which is what the client places it on. The
account id from the client's own command line appears inside the record, so the
roster is bound to the account rather than generic.

## Refuted

Kept because a plausible idea that was checked and failed is worth more than the
same idea rediscovered later.

* The server does **not** echo a client's position bit-identically. Two hand-picked
  samples said it did; measured across 513, only 106 matched.
* Bytes 7–8 of a movement message are **not** a duplicated heading. They agree
  about half the time, which is what noise looks like.
* `0x85/0x0114` does **not** place the character, despite its size and its position
  in the sequence. A bit-level scan finds nothing in it but `skin_unlock_amount_01`
  through `09`.
* The confirmed character id does **not** appear in the entity snapshot — not in
  any of three coherent sessions. Entities are not addressed by it.
* A roster ignored by the client is **not** an account mismatch: the client's own
  `-accid` is present in the record, at bit 48.

## Open

* What `0x84/0x00DD` contains (717 KB), and the record's numeric fields.
* The entity snapshot's block structure. Blocks are variable-length and tagged
  `5f 00`.
* What arms the Play button. Everything the real server sends before the click is
  now reproduced byte for byte, and the client still does not offer it.
* Why the selection screen renders a character with neither hair nor its equipped
  gear, when the roster it was given is byte-identical to the real one. The
  appearance may not come from this tier at all.

## Method

Two things did more for progress than any single protocol insight.

**Record your own traffic in the same format as the reference capture.** Diagnosing
a client that reaches a screen and stops had cost several rounds of guess, restart,
retest — one hypothesis per round trip. A recording that can be diffed against a
real session at message level tests every hypothesis at once, and found two
transport faults in the first two comparisons.

**Compare in ordering-index order, per connection.** Frame order misleads, merged
connections mislead worse. Both mistakes were made here and both produced confident
wrong conclusions.
