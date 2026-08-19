# Frida instrumentation for the Drakensang client

Passive capture told us the transport completely and left the two bulk gameplay
opcodes opaque. `0x8B/0x005F` has a fixed 15-byte body sent 19,270 times in a
15-minute session; nothing on the wire says how those 15 bytes are divided into
fields. Reading the code that *writes* them does say, which is what this is for.

## Read this before you start: you need Windows

The client is a **Windows x64 PE** — the addresses in the community's Ghidra
notes (`FUN_140c66458`) are based at `0x140000000`, which is the default image
base for one. Frida instruments Windows processes on Windows. Under Wine it does
not work in any dependable way: Frida would have to inject into Wine's emulation
of a PE, and there is no Linux Frida build that understands that.

Two workable setups:

* **Windows machine or VM** — install `frida-tools` there and run `hookcap.py`
  locally.
* **Windows VM plus your Linux host** — run `frida-server.exe` in the VM and
  pass `--host <vm-ip>` from Linux. Analysis and verification then happen on the
  machine where this repository lives.

`verify` and `analyse` need neither Frida nor the game, so the parts you iterate
on most are usable from Arch directly.

## The three layers

### 1. Socket capture — always works, no addresses needed

```bash
python hookcap.py capture --target Drakensang.exe -o session.jsonl
```

Hooks `ws2_32!sendto`, `recvfrom` and `WSASendTo`, and writes every UDP payload
as JSONL **in the same shape as `tests/fixtures/*.jsonl`**. Two things this has
that a pcap does not: the direction is known rather than inferred, and the peer
address is the real one instead of a NAT translation. It needs no capture
privileges either.

Then push it straight through the codec:

```bash
python hookcap.py verify session.jsonl
```

which reports parse failures, re-encode mismatches, unfinished split messages,
and a census of named application messages. A capture that does not verify is
not worth reasoning about.

### 2. Backtrace on a byte pattern — cheap, and often inconclusive

```bash
python hookcap.py capture --target Drakensang.exe \
    --pattern 8b5f00 --stacks bulk.jsonl -o session.jsonl
# then, in a second run with no --pattern, for a baseline:
python hookcap.py capture --target Drakensang.exe \
    --pattern 00 --stacks baseline.jsonl -o baseline-session.jsonl
python hookcap.py analyse bulk.jsonl --baseline baseline.jsonl
```

`analyse` ranks stack frames by how specific they are to the traced message:
frames on every send are RakNet's plumbing, frames only on the traced send are
the code that built it.

**Expect this to come up empty**, and read the verdict rather than the list.
RakNet buffers frames and flushes them from its own update tick, so the game's
serialiser is usually not on the stack at `sendto` at all. The tool says so in
that case instead of offering a shortlist that means nothing. When it does work
it saves an afternoon; when it does not, go to layer 3.

### 3. Offset hooks — the layer that actually answers the question

Find the function statically, then instrument it. `RakPeer::Send` is the target:
it is where the game hands a finished buffer to RakNet, so the serialiser is on
that stack and the buffer is intact.

```bash
python hookcap.py capture --target Drakensang.exe \
    --hook 'Drakensang.exe+0xc66dd0:RakPeerSend:args=6:dump=1:dumplen=32:bt' \
    --stacks send.jsonl -o session.jsonl
```

The spec is `module+0xoffset[:name][:args=N][:dump=N][:dumplen=N][:bt]`, where
`dump=1` hexdumps the second argument — for `RakPeer::Send(const char *data,
int length, ...)` that is the payload.

Offsets are **module-relative**, which is what a disassembler shows. Absolute
addresses are refused: ASLR moves the image every run, so `0x140c66dd0` would
hook the wrong place silently. Subtract `0x140000000`.

### Finding the address

`RakPeer::Send` has a recognisable shape even with no symbols. RakNet is BSD
licensed, so read the real source next to the disassembly rather than guessing.
Two anchors that work well:

* The **offline handshake constants** are in the binary verbatim. Search for the
  16-byte magic `00ffff00fefefefefdfdfdfd12345678`; the functions referencing it
  are RakNet's connection code, and `RakPeer` is right there.
* `RakPeer::Send` rejects a zero length and a null pointer at the top, then
  routes on a reliability enum of 0..7 — a small jump table on a value bounded by
  8 is a good fingerprint.

The community notes already name the dispatcher on the receive side
(`FUN_140c66dd0`, called for message IDs `0x84` and `0x85` with a flag). Hooking
that with `dump=1` gives decoded inbound payloads for free, which is the mirror
of the same trick.

## What comes out, and what to do with it

Once `RakPeer::Send` is dumping, the useful move is not to stare at the hexdump —
it is to hook the **`BitStream` writers** the serialiser calls just above it.
RakNet packs booleans into single bits and has `WriteCompressed` variants for
integers and floats, so field boundaries are not byte boundaries and no amount of
diffing hex will recover them. The *sequence of write calls* is the struct
definition. Rename those methods once in the disassembler and every message
handler becomes a readable list of fields.

That is the point at which `0x8B/0x005F`'s 15 bytes stop being 15 bytes.

## Scope

Everything here reads a process you own on a machine you own, to understand a
protocol well enough to reimplement the server side. It does not modify the
client, and nothing in this directory sends anything to Bigpoint's servers. The
emulator this feeds is meant to talk to its own client, which is both the useful
configuration and the defensible one.
