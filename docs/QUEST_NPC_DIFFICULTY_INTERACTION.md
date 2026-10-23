# Quest NPC interaction issues across map difficulties

## Scope

This guide covers quest NPCs that appear visible but cannot be clicked, fail
to start dialogue, or work in one map difficulty but not another. The failure
may affect any quest NPC; a visible scene model does not by itself confirm
that the server registered an interactable NPC actor.

## How the interaction path works

There are several separate steps between seeing a character and talking to
it:

1. The quest state or map event reveals the character in the client scene.
2. The server registers the network NPC actor, typically by sending `NewNPC`
   and its `NPCInfo` interaction metadata.
3. The client sends an interaction request referring to an actor ID.
4. If the request arrives before the actor is registered, some paths send
   actor ID `0`. The server may resolve that request to an eligible nearby
   quest NPC using the player's map, quest state, and distance.

These steps can get out of sync. A character may render due to a client-side
scene reveal even though its server actor or dialogue metadata is missing.
That is an interaction-registration issue, and does not automatically mean
the NPC failed to spawn or has an actor-ID collision.

## Why a problem can happen only in one difficulty

Difficulty variants may use different physical entry points, event state,
instance setup, or actor registration timing. A generic actor-zero fallback
also has a distance limit. If a particular difficulty places the player
farther from the authored NPC position than that limit, the request cannot
resolve to the quest NPC. Another difficulty may work because its flow brings
the player closer or registers the actor before the client sends the request.

For example, an NPC visible in two difficulties may still have different
server-side interaction readiness in each. Confirm the physical map ID,
entry position, event state, and actor registration for each difficulty
instead of assuming all variants share the same path.

## Confirmed example: Spiky Valley Normal

The confirmed case involved Wael (`h0006_quest_trader_wael_01`) in physical
map `h0006_valley_dun`. The server log reported:

```text
NPCRequest references unknown local actor 0
```

The Normal entry point was about 50 world units from Wael, while the generic
actor-zero fallback used a 24-unit radius. Wael was revealed by a client-only
quest scene event. The server's quest NPC registration path treated active
map-local events as server-managed and skipped the quest-driven `NewNPC`
registration for that client-only event. Consequently, the scene model could
be visible while the server could not resolve the early dialogue request.

The correction addressed both parts of that case:

- Quest refreshes now register revealed NPCs for client-only scene events;
  only server-driven map-local events suppress quest-driven registration.
- The actor-zero fallback uses a 64-unit approach radius on `h0006_valley_dun`
  to cover its roughly 50-unit Normal entry distance. Other maps retain the
  generic 24-unit radius.

This is an example of the general failure pattern, not a rule that every NPC
needs a larger radius. Set map-specific behavior only after measuring that
map's entry-to-NPC distance and confirming the relevant request path.

## Diagnostic workflow

For a quest NPC that fails in one or more difficulties:

1. **Identify the exact context.** Record the physical map ID, difficulty,
   active quest/task, player entry position, NPC template ID, and whether the
   NPC model is visible.
2. **Read the interaction logs.** Look for messages such as
   `NPCRequest references unknown local actor 0`, unresolved actor IDs, or
   rejected NPC interactions. Distinguish these from missing-spawn and
   actor-ID-collision evidence.
3. **Check reveal ownership.** Determine whether the NPC is revealed by a
   client-only scene event, a server-driven map event, or direct quest logic.
   Confirm which path is responsible for announcing its network actor.
4. **Check the actor payload.** Verify the player receives the expected
   `NewNPC` and `NPCInfo`, including the talk interaction and quest source,
   before the request is handled. Avoid sending duplicate actor IDs when a
   refresh runs more than once.
5. **Compare difficulty variants.** Inspect each variant's entry position,
   map access group, event flags, actor state, and timing. Measure the
   entry-to-NPC distance against any actor-zero fallback radius.
6. **Make the narrowest correction.** Repair the missing registration owner
   or add a map-specific resolution range only when evidence supports it.
   Avoid globally increasing all interaction ranges or changing actor-ID
   allocation as a first response.
7. **Add a regression check.** Use the failing difficulty's entry coordinates;
   verify that the correct quest NPC resolves, the dialogue metadata is
   emitted, and repeated refreshes do not duplicate the actor.

## Verification boundaries

Automated protocol tests can verify server-side actor resolution and emitted
interaction metadata. They cannot alone prove that the client renders the
NPC or that the dialogue opens in-game. Confirm the client behavior in the
affected difficulty after server-side checks pass.

Do not edit quest XML or other files under `data/` to address an interaction
registration issue unless the investigation shows the authored quest data is
actually incorrect. First separate scene visibility, network actor
registration, and request resolution; they are distinct parts of the flow.
