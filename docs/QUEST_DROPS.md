# Quest items and monster drops

The quest XML already links each task to its source monster and item. The server
was ignoring the ground-pickup stage: a successful drop roll recursively invoked
`collect`, even when the authored drop had `inventory="false"`.

## Authoritative association

For example, quest `a0002-WayToGrimmagstone-02-n` contains a task with:

```xml
<Event type="kill" value="a0003_gen_thing_roots_q_WayToGrimmagstone_02">
  <!-- The actual event also contains guards and a kill-counter Return. -->
  <Drop chance="1" value="quest_a0002_WayToGrimmagstone_02_wood"
        amount="1" inventory="false"/>
</Event>
<Event type="collect" value="quest_a0002_WayToGrimmagstone_02_wood">
  <Return value="a0002-WayToGrimmagstone-02-collect-01Collected"/>
</Event>
```

The enclosing quest supplies the owner. The event supplies the source monster;
`Drop` supplies the item, chance, amount, and destination. The item's appearance
and category come from `_Template_Item`. This mapping is explicit and does not
require guessing from item names or reconstructing ordinary monster loot lists.

The event-drop audit found:

- 5,623 kill-event drops with `inventory="false"` and 17 with `inventory="true"`.
- 5,684 ground-drop rules across all event types, including interactions,
  conversations, and deliveries.
- 877 distinct event-drop item IDs; 132 are used by more than one quest.
- 875 IDs match `_Template_Item` directly. `k0002_cremationashes` maps explicitly
  to the shipped `quest_k0002_cremationashes` item, preserving the original
  collect ID. `unique_questreward_protection`, used by `TestTriggeredQuest`, has
  no matching item; attempted materialization is logged without false credit.

These counts concern `<Task>/<Event>/<Drop>` rules. Quest-level acceptance grants
are separate.

## Runtime behavior

`apply_quest_event` now returns successful ground-drop rolls in
`QuestEventResult.drops`. It still evaluates the authored active-task guards,
chance, amount, and ordinary kill returns. A kill alone does not advance the
collection counter.

`QuestService` materializes those rolls through the existing `WorldLootDrop`,
`NewItem`, visibility, and pickup machinery. Each drop stores its owning
`quest_id` and authored `quest_collect_id` as server-only metadata. Different
quests may drop the same item template without crediting one another. Ordinary
unbound loot continues to feed the existing general collection hook.

Monster drops use the current death position from the shared combat state,
falling back to the authored monster position. Other event sources currently
use the player's position. Items use the shared map item-ID high-water mark and
are registered only in the owner's session. Cancellation removes uncollected
items for that quest. A cancellation or map transition during construction
prevents stale drops from being registered.

Collected items retain their owning `quest_id` in account saves, including after
stack splits and item-ID changes. Cancellation removes that quest's items from
bags, bank storage, ingredient storage, and hidden premium overflow. Inventory,
ground drops, and the quest log change together under the session lock; the client
receives inventory removal records and the account is saved. Forced map-exit
cancellation uses the same cleanup. Items owned by another quest are preserved,
even when the template is identical. Ordinary purchases and grants cannot merge
into a quest-owned stack.

For older saves without ownership, cleanup uses authored drop, collect, delivery,
and switch item references, limited to templates classified as `QuestItem`.
Items referenced by another open quest are retained until that quest is also
cancelled. This fallback preserves ordinary requirements such as purchased gems;
it cannot identify the owner of an ambiguous old shared stack.

Successful pickup adds the item to the existing inventory and then feeds
`collect` only to its owning quest. A rejected pickup (including full inventory)
keeps the drop and grants no progress. A repeated pickup request cannot claim
the same item twice. Picked-up items can also satisfy inventory-based delivery
tasks. Ground stacks respect the item's maximum stack size.

`inventory="true"` rules retain their previous immediate collection behavior.
Materializing those direct inventory grants, quest-level acceptance grants,
quest item stacking across separate pickups, and inventory cleanup at successful
quest turn-in are not implemented by this change. Ground loot uses the ordinary map
lifetime and is not persisted across reconnects; collected inventory and quest
progress use the existing persistence paths.

## Validation

`tests/test_quest_drops.py` covers native NewItem and pickup packets, deferred
credit, item ownership, shared-template quests, distinct IDs for same-map
players, death positions, failed and repeated pickups, cancellation (including
during construction), drop chances and amounts, stack limits, and the verified
cremation-ashes alias. Cancellation regressions cover every inventory container,
client removal packets, saved ownership, stack splitting, ordinary grants,
legacy shared items and aliases, refused and silent cancellations, and a pickup
interrupted by cancellation. The existing garden-quest replay now explicitly
picks up its eight wood drops.

```powershell
py -3.12-64 -m pytest tests/test_quest_drops.py tests/test_quests.py tests/test_quest_delivery.py tests/test_dungeon_quests.py tests/test_quest_events.py tests/test_quest_map_markers.py tests/test_premium_bags.py -q
```

Client rendering and clicking still require in-game validation. Restart the
server to load the changed quest and loot code. Already credited quest progress
is not rolled back.
