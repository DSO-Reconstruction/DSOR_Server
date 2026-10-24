import { livePage } from "./admin_live.js";
import { $, esc, human, date, classes, rarities, api, link, navigate, status, table, facts,
  section, tabs, toolbar, bindSearch, pager, tree, bookingTable, formError, notice, confirmAction } from "./admin_ui.js";

const containers = {equipment_slots: "Equipment", storage_58: "Inventory", storage_80: "Extra inventory", storage_a8: "Bank", storage_d0: "Extra bank"};
const slots = {0: "Helmet", 1: "Shoulders", 2: "Torso", 3: "Gloves", 4: "Boots", 5: "Main hand", 6: "Off hand", 7: "Back"};
const privileges = [["admin_privileges", "Game administrator"], ["admin_name_color", "Administrator name colour"]];

function privilegeSection(p) {
  return section("Game administrator privileges", `<form id="privilege-form" class="form-page">
    <fieldset class="form-section"><legend>Character flags</legend><div class="form-stack">
      <label class="check"><input name="admin_privileges" type="checkbox" ${p.admin_privileges ? "checked" : ""}>Game administrator</label>
      <label class="check"><input name="admin_name_color" type="checkbox" ${p.admin_name_color ? "checked" : ""}>Administrator name colour</label>
      <p class="help">The administrator flag carries the <code>!</code> chat commands and administrator-only client input; the colour only changes how the name looks to other players. Both are saved on the character and survive restarts, and can be set while offline. Chat rights update an online character immediately, but the game client reads the flag when it enters a map, so change map or relog for administrator input.</p>
    </div></fieldset>
    <p id="form-error" class="error-text" role="alert"></p>
    <div class="form-actions"><button type="submit">Save privileges</button></div></form>`);
}

function bindPrivileges(p, id) {
  $("#privilege-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.target, button = form.querySelector("button[type=submit]");
    const values = new FormData(form), changes = {};
    for (const [key, label] of privileges) {
      const granted = values.has(key);
      if (granted !== p[key]) changes[key] = granted;
    }
    button.disabled = true;
    try {
      if (!Object.keys(changes).length) throw new Error("No changes to save.");
      const summary = privileges.filter(([key]) => key in changes)
        .map(([key, label]) => `${label} ${changes[key] ? "granted" : "revoked"}`).join(", ");
      const reason = await confirmAction("Save game administrator privileges?", `${p.name}: ${summary}.`, "Save privileges");
      if (reason === null) return;
      await api(`/api/characters/${id}/privileges`, {changes, reason});
      notice(`Privileges saved: ${summary}.`);
      window.dispatchEvent(new Event("reload-page"));
    } catch (error) { formError(error); } finally { button.disabled = false; }
  });
}

function identitySection(p, range) {
  const level = range ? `<label>Level<input name="level" type="number" min="${range[0]}" max="${range[1]}" step="1" value="${p.level}"><small>This class can be between level ${range[0]} and ${range[1]}. Experience is set to the start of the new level.</small></label>`
    : `<label>Level<input name="level" type="number" value="${p.level}" disabled><small>This class has no authored levels to choose from.</small></label>`;
  return section("Name and level", `<form id="identity-form" class="form-page">
    <fieldset class="form-section"><legend>Identity</legend><div class="form-grid">
      <label>Name<input name="name" type="text" maxlength="32" value="${esc(p.name)}" autocomplete="off" spellcheck="false" dir="auto"><small>Up to 32 characters, no leading or trailing spaces. No other character may already use it.</small></label>
      ${level}
    </div>
      <p class="help">Works while the character is offline. Change only what you want to edit; the other field is left alone. Renaming updates the roster, the saved profile and chat, but other players see the new name when that character next enters a map. Moving to a different level resets its experience to that level's authored starting value and does not grant talents or recompute gear.</p>
    </fieldset>
    <p id="identity-error" class="error-text" role="alert"></p>
    <div class="form-actions"><button type="submit">Save name and level</button></div></form>`);
}

function bindIdentity(p, id, range) {
  $("#identity-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.target, button = form.querySelector("button[type=submit]");
    const values = new FormData(form), changes = {};
    button.disabled = true;
    try {
      const name = String(values.get("name"));
      if (name !== p.name) changes.name = name;
      if (range) {
        const level = Number(values.get("level"));
        if (!Number.isInteger(level) || level < range[0] || level > range[1])
          throw new Error(`Level must be a whole number between ${range[0]} and ${range[1]}.`);
        if (level !== p.level) changes.level = level;
      }
      if (!Object.keys(changes).length) throw new Error("No changes to save.");
      const summary = ["name" in changes ? `renamed to ${changes.name}` : "",
        "level" in changes ? `now level ${changes.level}` : ""].filter(Boolean).join(", ");
      const reason = await confirmAction("Save name and level?", `${p.name}: ${summary}.`, "Save name and level");
      if (reason === null) return;
      await api(`/api/characters/${id}/identity`, {changes, reason});
      notice(`Saved: ${summary}.`);
      window.dispatchEvent(new Event("reload-page"));
    } catch (error) {
      const target = $("#identity-error");
      if (target) { target.textContent = error.message; target.scrollIntoView({block: "nearest"}); }
      else notice(error.message, true);
    } finally { button.disabled = false; }
  });
}

function wisdomSection(wisdom) {
  const [minimum, maximum] = wisdom.range;
  return section("Wisdom Level", `<form id="wisdom-form" class="form-page">
    <fieldset class="form-section"><legend>Ancient Wisdom</legend><div class="form-grid">
      <label>Current Wisdom Level<input value="${wisdom.level}" disabled><small>${Number(wisdom.experience).toLocaleString()} Wisdom XP</small></label>
      <label>Set Wisdom Level<input name="level" type="number" min="${minimum}" max="${maximum}" step="1" value="${wisdom.level}" required>
        <small>Choose a level from ${minimum} to ${maximum}. The required Wisdom XP will be assigned.</small></label>
    </div></fieldset>
    <p id="wisdom-error" class="error-text" role="alert"></p>
    <div class="form-actions"><button type="submit">Set Wisdom Level</button></div></form>`);
}

function bindWisdom(wisdom, id) {
  $("#wisdom-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.target, button = form.querySelector("button[type=submit]");
    const values = new FormData(form), level = Number(values.get("level"));
    button.disabled = true;
    try {
      if (!Number.isInteger(level) || level < wisdom.range[0] || level > wisdom.range[1])
        throw new Error(`Wisdom Level must be a whole number between ${wisdom.range[0]} and ${wisdom.range[1]}.`);
      if (level === wisdom.level) throw new Error("Choose a different Wisdom Level.");
      const reason = await confirmAction("Set Wisdom Level?", `${wisdom.level} → ${level}.`, "Set level");
      if (reason === null) return;
      await api(`/api/characters/${id}/wisdom`, {level, reason});
      notice(`Wisdom Level set to ${level}.`);
      window.dispatchEvent(new Event("reload-page"));
    } catch (error) {
      const target = $("#wisdom-error");
      if (target) { target.textContent = error.message; target.scrollIntoView({block: "nearest"}); }
      else notice(error.message, true);
    } finally { button.disabled = false; }
  });
}

function honorSection(points) {
  return section("Honor points", `<form id="honor-form" class="form-page">
    <fieldset class="form-section"><legend>Career PvP honor</legend><div class="form-grid">
      <label>Current honor points<input value="${Number(points).toLocaleString()}" disabled></label>
      <label>Action<select name="mode"><option value="set">Set total</option><option value="increase">Increase by</option></select></label>
      <label id="honor-amount-label">New total<input name="amount" type="number" min="0" max="4294967295" step="1" value="${Number(points)}" required></label>
      <p class="help">Career PvP honor is separate from the spendable Badge of Honor currency. Changes are saved immediately and update the game client while the character is online.</p>
    </div></fieldset>
    <p id="honor-error" class="error-text" role="alert"></p>
    <div class="form-actions"><button type="submit">Save honor points</button></div></form>`);
}

function bindHonor(points, id) {
  const form = $("#honor-form"), mode = form.elements.mode, amount = form.elements.amount;
  const label = $("#honor-amount-label");
  mode.addEventListener("change", () => {
    label.firstChild.textContent = mode.value === "increase" ? "Increase by" : "New total";
    amount.value = mode.value === "increase" ? "0" : String(points);
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const button = form.querySelector("button[type=submit]"), values = new FormData(form);
    const selectedMode = String(values.get("mode")), value = Number(values.get("amount"));
    button.disabled = true;
    try {
      if (!Number.isSafeInteger(value) || value < 0 || value > 4294967295)
        throw new Error("Honor points must be a whole number from 0 to 4,294,967,295.");
      if (selectedMode === "increase" && value === 0)
        throw new Error("Enter an amount greater than zero.");
      const resulting = selectedMode === "set" ? value : Number(points) + value;
      if (resulting > 4294967295)
        throw new Error("Resulting honor points cannot exceed 4,294,967,295.");
      if (resulting === Number(points)) throw new Error("Honor points already have that value.");
      const description = selectedMode === "set" ? `${Number(points).toLocaleString()} → ${resulting.toLocaleString()}`
        : `add ${value.toLocaleString()} (${Number(points).toLocaleString()} → ${resulting.toLocaleString()})`;
      const reason = await confirmAction("Change honor points?", `${description}.`, "Save honor points");
      if (reason === null) return;
      await api(`/api/characters/${id}/honor-points`, {
        mode: selectedMode, amount: value, reason
      });
      notice(`Honor points updated to ${resulting.toLocaleString()}.`);
      window.dispatchEvent(new Event("reload-page"));
    } catch (error) {
      const target = $("#honor-error");
      if (target) { target.textContent = error.message; target.scrollIntoView({block: "nearest"}); }
      else notice(error.message, true);
    } finally { button.disabled = false; }
  });
}

function inventoryCopySection(id, itemCount) {
  return section("Copy items to another character", `<form id="inventory-copy-form" class="form-page">
    <fieldset class="form-section"><legend>Destination</legend><div class="form-stack">
      <label>Target character ID<input name="target_character_id" type="number" min="1" step="1" required>
        <small>Copies all ${itemCount} owned item records, including equipped and overflow items. Existing target items are kept.</small></label>
      <p class="help">The target must be online and fully loaded into a map. If the source is online, it must also be fully loaded; offline sources need saved gameplay state. Copies receive new item IDs. Equipped source items are added to available storage; no character progress or currencies are copied.</p>
    </div></fieldset>
    <p id="inventory-copy-error" class="error-text" role="alert"></p>
    <div class="form-actions"><button type="submit">Copy all items</button></div></form>`);
}

function bindInventoryCopy(id) {
  $("#inventory-copy-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.target, button = form.querySelector("button[type=submit]");
    const values = new FormData(form), targetId = Number(values.get("target_character_id"));
    button.disabled = true;
    try {
      if (!Number.isSafeInteger(targetId) || targetId <= 0)
        throw new Error("Enter a valid target character ID.");
      const reason = await confirmAction("Copy all items?",
        `Copy this character's full item inventory to online character ${targetId}? Existing target items will be kept.`,
        "Copy all items");
      if (reason === null) return;
      const result = await api(`/api/characters/${id}/copy-inventory`, {
        target_character_id: targetId, reason
      });
      notice(`Copied ${result.items_copied} items from ${result.source_name} to ${result.target_name}.`);
      window.dispatchEvent(new Event("reload-page"));
    } catch (error) {
      const target = $("#inventory-copy-error");
      if (target) { target.textContent = error.message; target.scrollIntoView({block: "nearest"}); }
      else notice(error.message, true);
    } finally { button.disabled = false; }
  });
}

function mainQuestSection() {
  return section("Main quests", `<form id="main-quests-form" class="form-page">
    <fieldset class="form-section"><legend>Quest progress</legend>
      <p class="help">Marks every World type main story quest and the Parallel Worlds introduction prerequisite as completed. No quest rewards are granted.</p>
    </fieldset>
    <p id="main-quests-error" class="error-text" role="alert"></p>
    <div class="form-actions"><button type="submit">Complete all main quests</button></div></form>`);
}

function bindMainQuests(id) {
  $("#main-quests-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.target, button = form.querySelector("button[type=submit]"), values = new FormData(form);
    button.disabled = true;
    try {
      const reason = await confirmAction("Complete all main quests?",
        "This will mark all main story quests and the Parallel Worlds introduction prerequisite as completed. Quest rewards will not be granted.",
        "Complete quests");
      if (reason === null) return;
      const result = await api(`/api/characters/${id}/complete-main-quests`, {reason});
      notice(`Marked ${result.newly_completed} main quests as completed.`);
      window.dispatchEvent(new Event("reload-page"));
    } catch (error) {
      const target = $("#main-quests-error");
      if (target) { target.textContent = error.message; target.scrollIntoView({block: "nearest"}); }
      else notice(error.message, true);
    } finally { button.disabled = false; }
  });
}

function ammonPWQuestSection() {
  return section("Ammon Parallel Worlds quests", `<form id="ammon-pw-quests-form" class="form-page">
    <fieldset class="form-section"><legend>Quest progress</legend>
      <p class="help">Completes and closes Ammon’s one-time Parallel Worlds quests and the map-gated unlock quests, including every task stage. Records permanent map access so the authored PW doors restore when the character enters the hub. Repeatable quests are skipped; quest rewards are not granted.</p>
    </fieldset>
    <p id="ammon-pw-quests-error" class="error-text" role="alert"></p>
    <div class="form-actions"><button type="submit">Complete Ammon’s Aradünya quests</button></div></form>`);
}

function bindAmmonPWQuests(id) {
  $("#ammon-pw-quests-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.target, button = form.querySelector("button[type=submit]"), values = new FormData(form);
    button.disabled = true;
    try {
      const reason = await confirmAction("Complete Ammon’s Parallel Worlds quests?",
        "This completes and closes Ammon’s one-time Aradünya quests and the follow-up quests required by Parallel Worlds map gates. Permanent map access is recorded so the hub doors can restore on entry. Repeatable quests are skipped and rewards are not granted.",
        "Complete Ammon quests");
      if (reason === null) return;
      const result = await api(`/api/characters/${id}/complete-ammon-pw-quests`, {reason});
      notice(`Completed ${result.quest_count} Ammon Aradünya quests and recorded ${result.gate_quest_count} map-gate unlocks.`);
      window.dispatchEvent(new Event("reload-page"));
    } catch (error) {
      const target = $("#ammon-pw-quests-error");
      if (target) { target.textContent = error.message; target.scrollIntoView({block: "nearest"}); }
      else notice(error.message, true);
    } finally { button.disabled = false; }
  });
}

function activeQuestSection(open) {
  return section("Active quests", `<form id="active-quests-form" class="form-page">
    <fieldset class="form-section"><legend>Quest progress</legend>
      <p class="help">Finishes every open quest in the quest log and hands it in as if the player had returned to the quest giver: quest items are removed and the usual XP, gold, Andermant and item rewards are granted. Follow-up quests unlocked by these hand-ins stay open. The character must be online and fully loaded into a map.</p>
      <p class="muted">${open} open quests in the last save.</p>
    </fieldset>
    <p id="active-quests-error" class="error-text" role="alert"></p>
    <div class="form-actions"><button type="submit">Complete all active quests</button></div></form>`);
}

function bindActiveQuests(id) {
  $("#active-quests-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.target, button = form.querySelector("button[type=submit]"), values = new FormData(form);
    button.disabled = true;
    try {
      const reason = await confirmAction("Complete all active quests?",
        "Every open quest in this character’s quest log is handed in and its rewards are granted. This cannot be undone.",
        "Complete quests");
      if (reason === null) return;
      const result = await api(`/api/characters/${id}/complete-active-quests`, {reason});
      notice(`Completed ${result.completed} active quests.` +
        (result.failed.length ? ` Not completed: ${result.failed.join(", ")}.` : ""), result.failed.length > 0);
      window.dispatchEvent(new Event("reload-page"));
    } catch (error) {
      const target = $("#active-quests-error");
      if (target) { target.textContent = error.message; target.scrollIntoView({block: "nearest"}); }
      else notice(error.message, true);
    } finally { button.disabled = false; }
  });
}

function bindInventoryItemDelete(id, item) {
  $("#inventory-item-delete-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.target, button = form.querySelector("button[type=submit]"), values = new FormData(form);
    button.disabled = true;
    try {
      const reason = await confirmAction("Delete this item?",
        `Delete ${item.template_id} (item ${item.item_id}, stack ${item.amount}) from this character?`,
        "Delete item");
      if (reason === null) return;
      const result = await api(`/api/characters/${id}/inventory/${item.item_id}/delete`, {reason});
      notice(`Deleted ${result.template_id} (item ${result.item_id}).`);
      navigate(`characters/${id}/inventory`);
    } catch (error) {
      const target = $("#inventory-item-delete-error");
      if (target) { target.textContent = error.message; target.scrollIntoView({block: "nearest"}); }
      else notice(error.message, true);
    } finally { button.disabled = false; }
  });
}

function location(item, inventory) {
  for (const [field, name] of Object.entries(containers)) {
    if (Object.hasOwn(inventory[field] || {}, item.item_id)) {
      const value = inventory[field][item.item_id];
      return {name, slot: field === "equipment_slots" ? value.map(s => slots[s] || `Slot ${s}`).join(", ") : `Slot ${Number(value) + 1}`};
    }
  }
  if ((inventory.field_180 || []).includes(item.item_id)) return {name: "Ingredients", slot: "—"};
  return {name: "Other", slot: "—"};
}

function characterTabs(id) {
  return [["Overview", `characters/${id}`], ["Inventory", `characters/${id}/inventory`],
    ["Progression", `characters/${id}/progression`], ["State", `characters/${id}/state`],
    ["Live actions", `characters/${id}/live`], ["Bookings", `characters/${id}/bookings`], ["Raw data", `characters/${id}/raw`]];
}

export async function characterPage(route) {
  const id = route.parts[1], tab = route.parts[2] || "overview";
  const data = await api(`/api/characters/${id}`), p = data.profile, s = data.state;
  const root = `characters/${id}`;
  const crumbs = data.account ? [["Accounts", "accounts"], [data.account.username, `accounts/${data.account.account_id}`]] : [["Characters", "characters"]];
  const result = {
    title: p.name, description: `${classes[p.class_id] || p.class_id} · Level ${p.level} · Character ${p.character_id} · ${data.online ? "Online" : "Offline"}`,
    breadcrumbs: [...crumbs, [p.name, root], ...(tab !== "overview" ? [[human(tab), `${root}/${tab}`, route.params]] : [])],
    actions: data.account && s ? link("New booking", "bookings/new", {account_id: data.account.account_id, character_id: id}, "button") : "",
    html: tabs(characterTabs(id), tab === "overview" ? root : `${root}/${tab}`)
  };
  if (tab === "live") return livePage(result, id);
  if (tab === "overview") {
    result.html += '<div class="columns">' + section("Character", facts([
      ["Account", data.account ? link(data.account.username, `accounts/${data.account.account_id}`) : "Unclaimed"],
      ["Character ID", p.character_id], ["Class", esc(classes[p.class_id] || p.class_id)], ["Level", p.level],
      ["Presence", status(data.online ? "Online" : "Offline", data.online ? "good" : "")],
      ["Saved map", `<code>${esc(s?.map_id || p.map_id)}</code>`], ["Last saved", esc(date(s?.saved_at))],
      ["Game admin privileges", status(p.admin_privileges ? "Granted" : "Not granted", p.admin_privileges ? "good" : "")],
      ["Admin name colour", p.admin_name_color ? "Yes" : "No"]
    ])) + section("Membership and PvP", facts([
      ["Membership", s?.membership?.active ? (s.membership.type === 2 ? "Deluxe" : `Type ${s.membership.type}`) : "Inactive"],
      ["Membership expires", esc(date(s?.membership?.expires_at))], ["PvP enabled", s?.pvp_enabled ? "Yes" : "No"],
      ["Honor points", esc(s?.honor_points ?? 0)], ["Wins / losses", `${s?.pvp_matches_won ?? 0} / ${s?.pvp_matches_lost ?? 0}`],
      ["Kills / deaths", `${s?.pvp_kills ?? 0} / ${s?.pvp_deaths ?? 0}`], ["Title", esc(s?.achievement_title || "None")]
    ])) + '</div>';
    result.html += identitySection(p, data.level_range) + privilegeSection(p);
    result.mount = () => { bindIdentity(p, id, data.level_range); bindPrivileges(p, id); };
    if (s) {
      result.html += wisdomSection(data.wisdom);
      result.html += honorSection(s.honor_points ?? 0);
      result.mount = () => { bindIdentity(p, id, data.level_range); bindWisdom(data.wisdom, id); bindHonor(s.honor_points ?? 0, id); bindPrivileges(p, id); };
      const balances = {...s.currencies}; delete balances.game;
      result.html += '<div class="columns">' + section("Currencies", table(["Currency", "Balance"], Object.entries(balances).map(([k, v]) => [esc(human(k)), esc(v)]))) +
        section("Additional currencies", table(["Currency", "Balance"], Object.entries(s.currencies.game || {}).map(([k, v]) => [esc(human(k)), esc(v)]))) + '</div>';
      result.html += `<p class="help">Values reflect the saved snapshot above. ${data.online ? "This character is online; values can change between saves." : ""}</p>`;
    } else result.html += '<p class="empty">No saved gameplay state. This character must enter the game first.</p>';
  } else if (tab === "bookings") {
    const bookings = await api(`/api/bookings?${new URLSearchParams({...route.params, character_id: id})}`);
    result.html += toolbar(route, "Search this character's bookings") + bookingTable(bookings.rows) + pager(bookings, route);
    result.mount = () => bindSearch(route);
  } else if (!s) {
    result.html += '<p class="empty">This character has not saved gameplay state yet.</p>';
  } else if (tab === "inventory") {
    if (route.parts[3]) return inventoryItemPage(result, route, data);
    const inventory = s.inventory, all = inventory.items || [];
    const itemCount = all.length + (inventory.premium_inventory_overflow || []).length +
      (inventory.premium_storage_overflow || []).length;
    const counts = {};
    for (const item of all) { const where = location(item, inventory).name; counts[where] = (counts[where] || 0) + 1; }
    let rows = all.filter(item => (!route.params.container || location(item, inventory).name === route.params.container) &&
      (!route.params.q || (item.template_id + " " + item.item_id).toLowerCase().includes(route.params.q.toLowerCase())));
    const offset = Math.max(0, Number(route.params.offset) || 0), total = rows.length;
    rows = rows.slice(offset, offset + 50);
    const options = ["", ...Object.keys(counts)].map(name => `<option value="${esc(name)}" ${route.params.container === name ? "selected" : ""}>${name ? `${esc(name)} (${counts[name]})` : "All containers"}</option>`).join("");
    result.html += `<p class="muted">${itemCount} owned item records · ${Object.keys(inventory.storage_58 || {}).length} / ${inventory.storage_size} inventory slots · ${Object.keys(inventory.storage_a8 || {}).length} / ${inventory.bank_storage_size} bank slots</p>` +
      inventoryCopySection(id, itemCount) +
      toolbar(route, "Search template or item ID", `<select id="container-filter" aria-label="Inventory container">${options}</select>`) +
      table(["Item", "Location", "Slot", "Rarity", "Level", "Stack", "Sockets"], rows.map(item => {
        const where = location(item, inventory);
        return [link(item.template_id, `${root}/inventory/${item.item_id}`, route.params, "resource mono") + `<span class="sub">Item ${item.item_id}</span>`,
          esc(where.name), esc(where.slot), esc(rarities[item.field_08] ?? item.field_08), esc(item.item_level), esc(item.amount),
          `${(item.socketed_templates || []).filter(Boolean).length} / ${item.socket_count}`];
      }), "No items match this filter.") + pager({total, offset, limit: 50}, route);
    result.mount = () => {
      bindInventoryCopy(id);
      bindSearch(route);
      $("#container-filter").addEventListener("change", event => navigate(route.path, {...route.params, container: event.target.value, offset: 0}));
    };
  } else if (tab === "progression") {
    result.html += '<div class="columns">' + section("Progression", facts([
      ["Level", p.level], ["Progression", esc(p.progression_u64)], ["Progression points", esc(p.progression_u32)],
      ["Talent category points", esc((s.talent_category_points || []).join(" / "))],
      ["Unlocked chapters", esc((s.chapters_unlocked || []).join(", ") || "None")],
      ["Unlocked maps", esc((s.unlocked_maps || []).join(", ") || "None")]
    ])) + section("Talent selections", tree(s.talent_categories)) + '</div>';
    result.html += section("Quest log", records(s.quest_log, "Quest")) + section("Achievements", records(s.achievements, "Achievement")) +
      '<div class="columns">' + section("Events", tree({progress: s.event_progress, participation: s.event_participation})) +
      section("Talent presets", tree({skills: s.skill_talent_presets, categories: s.category_talent_presets})) + '</div>';
    result.html += mainQuestSection() + ammonPWQuestSection();
    result.mount = () => { bindMainQuests(id); bindAmmonPWQuests(id); };
  } else if (tab === "state") {
    result.html += '<div class="columns">' + section("Position and map access", tree({map: s.map_id, position: s.position, last_visited_town: s.last_visited_town,
      map_access: s.map_access_id, map_difficulty: s.map_difficulty, scaling_difficulty: s.scaling_difficulty, unlocked_maps: s.unlocked_maps})) +
      section("Vitals and appearance", tree({health: s.inventory.health_points, resource: s.inventory.secondary_resource,
        costume: s.active_costume, pet: s.active_pet, decorations: s.active_decorations, mount: s.mount_ride})) + '</div>' +
      section("Active status effects", tree(s.active_status_effects)) + '<div class="columns">' +
      section("Collections and crafting", tree({stickerbook: s.stickerbook_entries, recipes: s.recipe_book_resources})) +
      section("Quickbars and offers", tree({quickbar: s.quickbar, slots: s.quick_slots, offer_purchases: s.timed_offer_purchases})) + '</div>';
    const open = Object.values(s.quest_log || {}).filter(q => q.state === 0 || q.state === 1).length;
    result.html += activeQuestSection(open);
    result.mount = () => bindActiveQuests(id);
  } else if (tab === "raw") {
    result.html += '<p class="muted">Complete saved documents for diagnostics. Read only.</p>' +
      section("Character profile", `<pre>${esc(JSON.stringify(p, null, 2))}</pre>`) +
      section("Saved state", `<pre>${esc(JSON.stringify(s, null, 2))}</pre>`);
  } else throw new Error("Character page not found.");
  return result;
}

function records(document, label) {
  return table([label, "Details"], Object.entries(document || {}).map(([id, value]) => [
    `<code class="resource">${esc(id)}</code>`, typeof value === "object" ? tree(value) : esc(value)
  ]));
}

function inventoryItemPage(result, route, data) {
  const inventory = data.state.inventory;
  const item = inventory.items.find(i => String(i.item_id) === route.parts[3]);
  if (!item) throw new Error("This item is no longer present in the saved inventory.");
  const where = location(item, inventory);
  result.title = item.template_id;
  result.description = `${data.profile.name} · Item ${item.item_id} · ${where.name}`;
  result.breadcrumbs.push([String(item.item_id)]);
  result.actions = link("Item template", `items/${encodeURIComponent(item.template_id)}`, {account_id: data.account?.account_id, character_id: data.profile.character_id}, "button secondary");
  result.html += '<div class="columns">' + section("Item properties", facts([
    ["Item ID", item.item_id], ["Template", `<code class="resource">${esc(item.template_id)}</code>`],
    ["Rarity", esc(rarities[item.field_08] ?? item.field_08)], ["Item level", item.item_level], ["Stack size", item.amount],
    ["Container", esc(where.name)], ["Slot", esc(where.slot)]
  ])) + section("Values", facts([
    ["Sockets", item.socket_count], ["Occupied sockets", (item.socketed_templates || []).filter(Boolean).length],
    ["Sale value", esc(item.field_30)], ["Melt value", esc(item.field_34)],
    ["Created", esc((item.created_at || []).join(" / "))]
  ])) + '</div>';
  result.html += section("Enchantments", table(["Enchantment", "Roll quality", "Tier", "Level", "Rarity"], (item.enchantments || []).map(e => [
    `<code class="resource">${esc(e.template_id)}</code>`, `${(e.value * 100).toFixed(1)}%`, e.field_08, e.field_0c, esc(rarities[e.kind] ?? e.kind)
  ]), "No enchantments on this item."));
  result.html += section("Sockets", table(["Socket", "Content"], Array.from({length: item.socket_count}, (_, index) => [index + 1,
    item.socketed_templates[index] ? link(item.socketed_templates[index], `items/${encodeURIComponent(item.socketed_templates[index])}`, {}, "resource mono") : '<span class="muted">Empty</span>'
  ]), "This item has no sockets."));
  result.html += section("Delete item", `<form id="inventory-item-delete-form" class="form-page">
    <fieldset class="form-section"><legend>Remove from character</legend>
      <p class="help">This permanently removes this item record and its equipment or storage allocation.</p>
    </fieldset>
    <p id="inventory-item-delete-error" class="error-text" role="alert"></p>
    <div class="form-actions"><button type="submit" class="danger">Delete item ${item.item_id}</button></div></form>`);
  result.mount = () => bindInventoryItemDelete(data.profile.character_id, item);
  return result;
}
