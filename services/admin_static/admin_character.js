import { livePage } from "./admin_live.js";
import { $, esc, human, date, classes, rarities, api, link, navigate, status, table, facts,
  section, tabs, toolbar, bindSearch, pager, tree, bookingTable } from "./admin_ui.js";

const containers = {equipment_slots: "Equipment", storage_58: "Inventory", storage_80: "Extra inventory", storage_a8: "Bank", storage_d0: "Extra bank"};
const slots = {0: "Helmet", 1: "Shoulders", 2: "Torso", 3: "Gloves", 4: "Boots", 5: "Main hand", 6: "Off hand", 7: "Back"};

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
      ["Game admin privileges", p.admin_privileges ? "Yes" : "No"]
    ])) + section("Membership and PvP", facts([
      ["Membership", s?.membership?.active ? (s.membership.type === 2 ? "Deluxe" : `Type ${s.membership.type}`) : "Inactive"],
      ["Membership expires", esc(date(s?.membership?.expires_at))], ["PvP enabled", s?.pvp_enabled ? "Yes" : "No"],
      ["Honor points", esc(s?.honor_points ?? 0)], ["Wins / losses", `${s?.pvp_matches_won ?? 0} / ${s?.pvp_matches_lost ?? 0}`],
      ["Kills / deaths", `${s?.pvp_kills ?? 0} / ${s?.pvp_deaths ?? 0}`], ["Title", esc(s?.achievement_title || "None")]
    ])) + '</div>';
    if (s) {
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
    const counts = {};
    for (const item of all) { const where = location(item, inventory).name; counts[where] = (counts[where] || 0) + 1; }
    let rows = all.filter(item => (!route.params.container || location(item, inventory).name === route.params.container) &&
      (!route.params.q || (item.template_id + " " + item.item_id).toLowerCase().includes(route.params.q.toLowerCase())));
    const offset = Math.max(0, Number(route.params.offset) || 0), total = rows.length;
    rows = rows.slice(offset, offset + 50);
    const options = ["", ...Object.keys(counts)].map(name => `<option value="${esc(name)}" ${route.params.container === name ? "selected" : ""}>${name ? `${esc(name)} (${counts[name]})` : "All containers"}</option>`).join("");
    result.html += `<p class="muted">${all.length} item records · ${Object.keys(inventory.storage_58 || {}).length} / ${inventory.storage_size} inventory slots · ${Object.keys(inventory.storage_a8 || {}).length} / ${inventory.bank_storage_size} bank slots</p>` +
      toolbar(route, "Search template or item ID", `<select id="container-filter" aria-label="Inventory container">${options}</select>`) +
      table(["Item", "Location", "Slot", "Rarity", "Level", "Stack", "Sockets"], rows.map(item => {
        const where = location(item, inventory);
        return [link(item.template_id, `${root}/inventory/${item.item_id}`, route.params, "resource mono") + `<span class="sub">Item ${item.item_id}</span>`,
          esc(where.name), esc(where.slot), esc(rarities[item.field_08] ?? item.field_08), esc(item.item_level), esc(item.amount),
          `${(item.socketed_templates || []).filter(Boolean).length} / ${item.socket_count}`];
      }), "No items match this filter.") + pager({total, offset, limit: 50}, route);
    result.mount = () => {
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
  } else if (tab === "state") {
    result.html += '<div class="columns">' + section("Position and map access", tree({map: s.map_id, position: s.position, last_visited_town: s.last_visited_town,
      map_access: s.map_access_id, map_difficulty: s.map_difficulty, scaling_difficulty: s.scaling_difficulty, unlocked_maps: s.unlocked_maps})) +
      section("Vitals and appearance", tree({health: s.inventory.health_points, resource: s.inventory.secondary_resource,
        costume: s.active_costume, pet: s.active_pet, decorations: s.active_decorations, mount: s.mount_ride})) + '</div>' +
      section("Active status effects", tree(s.active_status_effects)) + '<div class="columns">' +
      section("Collections and crafting", tree({stickerbook: s.stickerbook_entries, recipes: s.recipe_book_resources})) +
      section("Quickbars and offers", tree({quickbar: s.quickbar, slots: s.quick_slots, offer_purchases: s.timed_offer_purchases})) + '</div>';
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
  return result;
}
