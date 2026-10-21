# Shadow Soul effect delivery

## Local evidence

Inspected the bundled SQLite tables and the supplied native client without
modifying either artifact. This is static analysis, not a captured retail run.

- `data/db/static.db4` SHA-256:
  `934702e7f7b392df0ba91a91868b5a36c36210ced97a417da3bd4f3f3b5662dd`.
- `DRASA/cdndata_Client/dlcache/dro_client64.exe` SHA-256:
  `da2fa5b50ffb22fd05f115a21b223d50e4e520214bc10577bf1d98b130b4bf14`.
- Native disassembly: `0x1400bd73c` registers the condition associated with
  `mercenary_ember_knight_heat`; `0x140a7b3c2` resolves `maxstacksizeheat`.
  These establish the client-side condition identity, not live activation.

## Ember Knight Kain

`_Template_Skill.VictimStatusEffects` contains both heat effects, but their
`_Template_StatusEffect.EffectHosts` is **Causer**. Routing every entry to the
struck victim lost the minion's authoritative heat state and sent its stacks
to the wrong actor. Lethal hits still qualify as successful hits.

`mercenary_ember_knight_heat` lasts five seconds and caps at 20 stacks. Its
`Speed:0.05,relative,Skills` is applied per stack. The companion
`mercenary_ember_knight_heat_onlypve` supplies `Resistance:0.05,relative` for
all six authored damage types; its `DisabledMatchPvP` group excludes it from
PvP applications. A fresh successful hit renews the lifetime. After a gap
longer than the lifetime the next hit starts a new stack generation.

The two-piece `mercenary_SoulLava_<class>` item-set tier supplies
`mercenary_ember_knight_heatmax_aura`, targeting Minion. Its child registers
`ON:maxstacksizeheat` to apply `mercenary_ember_knight_overheated`, which
unlocks hidden `mercenaryskill_lava_01_attack_02`. The overheat gate is checked
against the owner's active set sources, rather than unconditionally unlocking
the hidden attack after 20 lifetime hits.

Successful summon impacts now return Causer applications separately from
victim effects. Each viewer receives native status create/replace commands
for Kain. Combat retains the capped gameplay stack count and applies the
per-stack speed scale to both the attack interval and impact delay, including
the execution context's hit/unblock frames. Scaling only the interval leaves
the pending attack blocking the next cast for the original, unscaled hit time.

Headless Ghidra analysis of `ClientActorStatusEffectModule::AddEffect`
(`0x1401eaa70`) shows that explicit stack indices at or above `MaxStackSize`
are rejected. Causer stack indices therefore stay in `0..19`. At the cap,
the existing instance is renewed using the no-details `field_25=true` branch
in `ClientStatusEffectManager::HandleStatusEffectCommand` (`0x140354b98`),
without sending a stale-instance deletion. Its native branch refreshes the
instance and updates its duration. Ordinary effects retain their existing
occurrence handling.

The bundled heat effects have `Special` / `DisabledMatchPvP` groups, without
`BuffUI`, and are hosted on the attacking minion. These rows do not define a
heat icon or resistance bonus on the owning player's buff bar. Such a player
UI change would require separate client content rather than falsely applying
the minion's stat bonuses to the player.

## Divine Wanderer / holy field

`mercenary_divine_wanderer_aura_effect` adds `holysoul_<mobility>_minion` as a
user effect on the four classes' mobility skills. An `addUE` without `ON`
must use the skill-start default. Treating it as a victim-hit registration
prevented Teleport, which needs no enemy hit, from creating its field.

Each status creates a ten-second `minion_holysoul_<mobility>`. Its permanent
statuses contain two radius-four, height-100 auras: one targeting Summoner,
the other Enemies. Status minions are now positioned at the absolute mobility
destination, and their authored auras become authoritative spatial effects.
The spatial skill handler must pass its saved `location_position_world` into
status-minion creation: its `target_position` has already been converted to
wire coordinates by that point. Mixing these offsets displaces the field on
every map whose coordinate origin is nonzero.

The owner child includes +100% maximum health, +10% relative resistance,
+5 absolute skill-resource regeneration, class/set-variable modifiers and
20% relative-health healing ticks. Its modifiers are installed as a player
runtime status while inside the field. Leaving or field expiration removes
that status and cancels its healing timer. Overlapping fields retain one
owner effect with the latest applicable expiry.

The enemy child ticks `CurrHealthPointsDmg:-2.0,relative` with
`DamageDoneLightning`. Managed receiver leases are renewed while a monster
remains in the field; the existing DoT cadence is preserved across renewal.

## Shadow Stalker / Destruction Mark

`mercenary_shadow_stalker_aura_effect` registers a 15% critical-hit user proc.
`mercenary_shadow_stalker_DesolationImpact_trigger` has a 30-second duration
and `ExclusivityType:Deny`. Only an accepted instance activates its children.
Its immediate LE supplies the charging visual; its AE is delayed by 0.5s
and creates an enemy aura of radius six, lasting three seconds.

The enemy receiver deals `-2` relative physical damage and immediately adds
`debuff_DestructionMark` for eight seconds. That mark supplies
`Speed:-0.1,relative,Movement`. The server now resolves the receiver's nested
immediate status instructions instead of hardcoding a detached marker.

Immediate AE/LE activation belongs to accepted user-status delivery, covering
all cast/impact paths. It also works before the player's first movement packet
by falling back to the spawn position. Delayed children have distinct timer
keys so one child cannot replace another child's timer.

## Verification

`tests/test_shadow_soul_effects.py` covers actual summon impact resolution,
lethal-hit stacking, expiry, maximum stacks, item-set gating, native stack
entries, Teleport's skill-start registration and destination, holy-field
membership/stat changes/healing cleanup, lightning DoT, critical-only proc
selection, deny cooldown and delayed damage/mark receiver expansion.

Run `python -m pytest tests -q`. Live client rendering still needs confirmation
after the server process loads the updated code.
