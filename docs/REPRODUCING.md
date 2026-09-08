# Reproducing a session

What it takes to get the retail client from a login prompt to standing on a map
against this server, and how to gather the evidence if it stops somewhere.

## What you need

* The retail Windows client. This repository contains none of it.
* A Windows machine or VM that can reach the host running the server. The setup
  these notes were written against is a Windows VM whose host is visible at
  `172.20.0.1` and whose Linux home is mounted as `\\host.lan\Data`.
* Python 3.11+ on the server host. No third-party packages are required to run the
  server; `pytest` runs the tests, and `frida` is needed only for client-side
  capture.
* The client's own launch arguments. The launcher passes about twenty, and the one
  that matters is `-ip`, which decides which login server the client dials — so no
  patching and no DNS games are needed.

## Running the server

```sh
python server.py --advertise 172.20.0.1 -v --capture ~/session.jsonl
```

`--advertise` is the address written into every handoff, and it has to be reachable
**by the client**. Behind a VM that is not an address this process can see on
itself, so it cannot be detected and must be given.

`--capture` records every datagram in the same format as a reference capture, which
is what makes a failing session diffable against a working one. Use it.

Other flags worth knowing:

| flag | why |
|---|---|
| `--map` | which map the map server serves; default is the tutorial dungeon |
| `--schedule-rate` | datagrams a second for the 717 KB event schedule. The default is the measured rate. Raising it is how to confirm that a fast schedule still crashes the client |
| `--login-port` `--character-port` `--map-port` | for running two instances side by side |

## Pointing the client at it

The client needs its real launch arguments with `-ip` replaced. The PowerShell
driver in `tools/frida/` does that, and also attaches Frida so the client's own view
of the traffic is captured:

```powershell
powershell -ExecutionPolicy Bypass -File \\host.lan\Data\hook-dso.ps1 `
  -Spawn -CmdFile \\host.lan\Data\dso\cmd.txt -LoginIp 172.20.0.1:2190
```

`-CmdFile` is a file holding the client's command line; `-ShowArgs` prints it from a
running client so you can create that file rather than retyping twenty arguments.
`-SocketOnly` skips the function hook and captures UDP payloads only, which is the
more reliable mode if Frida objects to anything.

**That file holds a live session credential and the CDN root key.** Keep it out of
version control — the `.gitignore` here already excludes `cmd.txt` — and expect to
refresh it: launch the game once through the official launcher, then `-ShowArgs`.

## Accounts, and playing two clients at once

The client never sends a password. The capture is unambiguous about it: twelve `0x8A`
messages, one per tier per connection, each carrying an account id (`-accid`) and a
16-byte session GUID (`-sid`) and nothing else, byte-identical across all twelve of a
session. So the session id **is** the credential a game server can check, the password
is checked somewhere off the wire, and the launcher line is how the credential reaches
the client.

Which means an account has to be issued before it can play. Two ways:

```
python3 server.py account add Username <password> --character Username --class warrior
python3 server.py account list
```

or the sign-up page, which the server starts when `portal_port` is set (it is, in
`dsor.toml`, on 8080): register, and it hands back the launcher line with `-accid` and
`-sid` already filled in. It is plain HTTP and binds to the loopback, because a public
address would put passwords on the wire in the clear.

Both print a launcher line. Take the `-accid` and `-sid` out of it and put them in the
real one from `cmd.txt`, keeping that file's `-rooturl`, `-cdnurl` and `-rootkey`: those
are CDN settings and never reach a game server, so they have to stay as the launcher
made them.

Then:

* **the selection screen shows the account's own characters**, built by
  `dsor/charlist.py` rather than replayed. An account with no characters falls back to
  the recorded roster, which is what showed everybody Username.
* **the name over the head comes from the store**, through `playerstate.with_name`. Two
  accounts are two names; before this, two clients were both Username.
* **one account plays from one client.** A second `0x8A` naming an account somebody else
  is already holding is refused with a `DISCONNECTION_NOTIFICATION` — silence is worse,
  an ignored client reconnects for ever. A claim a killed client never released expires
  after `Sessions.linger` seconds, or the account would be locked out until a restart.
  `--shared-account` turns the rule off.
* an account the store **does not** know is admitted anyway, which is what keeps the
  recorded launcher line working. `--require-account` closes that.

Signing in again issues a new `-sid` and the previous launcher line stops working. That
is the only revocation there is, and it is worth having: the session id is the whole
credential.

## Reading what happened

Four artefacts, and each answers a different question.

**The server log** says what the server decided. Which tier the client reached, what
it was sent, what it asked for again.

**The `--capture` recording** says what actually went on the wire. Compare it to a
reference capture *in ordering-index order and per connection* — frame order
misleads, and merged connections mislead worse, because ordering indices restart
with every connection. A gap in the ordered stream is fatal and silent: a peer
cannot deliver anything past a missing index, so the symptom is a client frozen on
whatever screen it had already reached.

**The client's log** says what the client thinks. It is the only source for what it
is waiting for, and it names its own stages: `Connection to server established!`,
`Handle login challenge!`, `Received switch map command (host:port)`, `Send client
ready packet!`, `Syncing client game world with time sync packet: N server game
tick, N delay!`, `handle event updates!`. A Nebula assertion in it names the thing
that was missing.

**The Frida `calls-*.jsonl`** says what the client did internally. Its most useful
trick so far was catching the crash reporter serialising an
`EXCEPTION_ACCESS_VIOLATION` and its callstack — which is how a client that appeared
to hang turned out to be dying.

## Testing without the client

```sh
python -m pytest -q
```

The suite needs no game and no network. Two parts of it are worth knowing about.

`tests/test_replay_character_flow.py` drives a real `Service` with every datagram
the recorded client sent, in-process, and inspects the answers. It reaches the
grant and the handoff — branches the live client refuses to reach while anything
earlier is broken — and its contiguity check over the ordered stream is what catches
the fault class that costs a manual round trip to find otherwise.

`tests/test_known_bugs.py` is one test per mistake that has actually been made here,
each named after its symptom and carrying the measurement that settled it. Several
of them exist because the obvious reading of a capture was wrong.

## Capturing the live service, usefully

A capture answers the question it was made for and no other, so decide the question
first. Two mistakes cost whole rounds here.

**Capture from the client's launch, not by attaching to a running one.**
`-SocketOnly` attaches to a client that is already up, so the login, the character
roster and the player initialisation are already past — and those carry the level, the
experience, the andermant and the inventory. `-Spawn` launches the client under Frida
and follows the launcher down to it:

```
powershell -ExecutionPolicy Bypass -File \\host.lan\Data\dsor-server\tools\frida\hook-dso.ps1 `
    -Spawn -CmdFile \\host.lan\Data\dso\cmd.txt -OutDir \\host.lan\Data\dso-capture\une-question
```

The session token in `cmd.txt` expires, so refresh it from a normal launch and run this
straight after. `-OutDir` into its own directory: there are 161 `session-*.jsonl` in
`~/dso-capture` and the timestamp in the name comes from the Windows clock, which is
hours off the host's.

**Isolate what you want to measure.** Casting two skills within ten seconds puts both
sets of effects in one message, because a `StatusEffectCommand` carries an actor's whole
list — which is how "casting Dragon Hide shows Power of Smash" was misdiagnosed for a
week. Thirty seconds between casts, and nothing else in between.

**Write down what the screen said.** A level, a character's name, an amount of
andermant: given a value, a field can be found *by its value* in a bit-packed message of
631 KB. Without one, the method is to spot a plausible pattern and conclude, and that
broke the login twice. The andermant's offset came from one line — "dans l'autre capture
j'etais a 4814 andermant" — against a dump that had been sitting unread for hours.

## Measuring against the live service and not against yourself

Half of the 144 captures in `~/dso-capture` are **this server's own output**. Any
measurement taken across all of them measures this server's mistakes back as ground
truth: the status effect table's wire index was verified that way for weeks, once
against 41 candidate offsets, and was wrong the whole time.

`tools/effect_tables.py` classifies a capture before reading it, by two markers:
`a0001_tutorial_heal_on_low_health` was replayed on the player by the old emulator 25
times a second, while `skill_frenzyshout_buff_lifeleech` and warshout's named buffs only
ever came from the live service. A capture bearing the first is dropped, even if it also
bears the second.

## Reading the client itself

`dso/DSOClient/dlcache/dro_client64.exe` carries every function signature as a plain
string — `bool __cdecl Skills::SkillTemplate::AddEffect(...)` and so on — along with the
file and line of every assertion. That makes it searchable without symbols:

* find a diagnostic string with `pefile`, then find the `lea reg, [rip+disp]` that
  resolves to it by scanning `.text` for `48 8d` / `4c 8d` with `mod=00, rm=101`;
* disassemble around it with `capstone`. Full analysis in radare2 on a 21 MB binary
  times out; the targeted scan takes seconds.

This is how the status effect handler's control flow, its four rejection messages, its
five-tick clock tolerance and its silent per-element skip were established. See the
README.

## When a capture disagrees with this repository

Trust the capture, and check three things before trusting your reading of it.

Are you looking at the ordering index or the frame order? Several messages share a
datagram, and a split message's fragments interleave with everything else.

Are you keeping connections apart? The client opens a new connection for each tier
and returns to the login server between zones, and every index restarts.

Are you counting distinct reliable indices, or occurrences? Twenty-three copies of
one reliable index is one message being retransmitted because you did not
acknowledge it — not twenty-three requests.
