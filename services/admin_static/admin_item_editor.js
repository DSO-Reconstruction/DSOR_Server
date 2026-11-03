import { $, esc, rarities, api, section, status, notice, announce, confirmAction, debounce } from "./admin_ui.js";

// Edits an equipment item's enchantments, their tiers and its base-stat roll.
// Every change is previewed by the server, which computes values with the
// combat code, so the numbers shown are the ones the game applies.

const percent = value => `${(Number(value) * 100).toFixed(1).replace(/\.0$/, "")}%`;
const range = ([low, high]) => low === high ? percent(low) : `${percent(low)}–${percent(high)}`;
const tierLabel = (tier, factors) => `Tier ${tier.tier}${tier.name ? ` · ${tier.name}` : ""} · ${range(factors)}`;
const editable = ({template_id, value, tier, kind, origin_item_level}) => ({template_id, value, tier, kind, origin_item_level});

export function itemEditorSection(doc) {
  if (!doc.editable) return "";
  const notes = (doc.online ? status("Online", "good") : status("Offline")) + " · " + [
    doc.online ? "Saving sends the item to the game client at once." : "Saving updates the character's save.",
    doc.equipped && doc.online ? "Equipped, so combat stats are recalculated on save." : ""].filter(Boolean).join(" ");
  return section("Stats editor", `<form id="item-editor" class="item-editor" novalidate>
    <p class="help editor-note">${notes} If an open tooltip still shows old values, close it or change map.</p>
    <fieldset class="form-section"><legend>Base stats</legend>
      <div class="form-grid">
        <label>Base tier<select name="base_tier"></select><small>Bounds every tier-scaled base stat for this ${esc(doc.item.rarity_name)} item.</small></label>
        <div class="roll-field"><label for="base-target">Base roll</label>
          <div class="roll-control"><input id="base-target" name="base_target" type="range" min="0" max="100" step="1" aria-describedby="base-target-help">
            <input name="base_target_number" type="number" min="0" max="100" step="1" aria-label="Base roll percent"><span aria-hidden="true">%</span></div>
          <small id="base-target-help">Picks the closest of the 16,384 rolls the game can give this item. The first search takes a few seconds.</small></div>
      </div>
      <div id="base-stats"></div>
      <p class="help seed-line">Roll seed <input name="base_seed" type="number" min="1" max="${doc.options.max_seed}" step="1" aria-label="Base roll seed">
        <button type="button" class="secondary" data-action="random-seed">Random roll</button></p>
    </fieldset>
    <fieldset class="form-section"><legend>Enchantments</legend>
      <div class="toolbar enchant-tools">
        <button type="button" class="secondary" data-action="max-rolls">Max every roll</button>
        <select name="all_tiers" aria-label="Tier for every enchantment"></select>
        <button type="button" class="secondary" data-action="apply-tier">Set every tier</button>
        <span class="spacer"></span><span class="list-count" id="enchant-count"></span>
      </div>
      <ol id="enchant-list" class="enchant-list"></ol>
      <p class="help">${doc.options.rolled_count == null ? "This rarity carries a fixed enchantment set." : `${esc(doc.item.rarity_name)} items roll ${doc.options.rolled_count} random enchantment${doc.options.rolled_count === 1 ? "" : "s"}.`}
        The list holds what the game may put on this item: its slot, class, level and the affinities its base stats provide. Up to ${doc.options.max_enchantments} are allowed.</p>
      <button type="button" class="secondary" data-action="add">Add enchantment</button>
    </fieldset>
    <p id="item-editor-error" class="error-text" role="alert"></p>
    <div class="editor-actions"><span id="editor-state" class="muted" aria-live="polite">No changes</span>
      <button type="button" class="secondary" data-action="reset">Reset changes</button>
      <button type="submit">Save item</button></div>
  </form>`);
}

export function bindItemEditor(doc, characterId) {
  const form = $("#item-editor");
  if (!form) return;
  const path = `/api/characters/${characterId}/inventory/${doc.item.item_id}/editor`;
  const options = doc.options, tiers = new Map(options.tiers.map(tier => [tier.tier, tier]));
  const choices = new Map(options.enchantments.map(choice => [choice.template_id, choice]));
  const duplicateNames = new Set(options.enchantments.map(c => c.name).filter((name, i, all) => all.indexOf(name) !== i));
  const saved = {base_enchantment_tier: doc.base.tier, base_seed: doc.base.seed, enchantments: doc.enchantments.map(editable)};
  let draft = structuredClone(saved), target = null, request = 0, pending = null;
  const fields = form.elements;

  const choiceLabel = choice => {
    const label = choice.summary ? `${choice.name} — up to ${choice.summary}` : choice.name;
    return duplicateNames.has(choice.name) ? `${label} (${choice.template_id})` : label;
  };
  const tierOptions = (kind, selected) => options.tiers.map(tier =>
    `<option value="${tier.tier}" ${tier.tier === selected ? "selected" : ""}>${esc(tierLabel(tier, tier.factors[kind] || tier.factors[0]))}</option>`).join("");

  fields.base_tier.innerHTML = options.base_tiers.map(tier =>
    `<option value="${tier.tier}">${esc(tierLabel(tier, tier.range))}</option>`).join("");
  fields.all_tiers.innerHTML = options.tiers.map(tier => `<option value="${tier.tier}">Tier ${tier.tier}${tier.name ? ` · ${esc(tier.name)}` : ""}</option>`).join("");
  const rolled = doc.base.stats.filter(stat => stat.roll !== null);
  const startQuality = rolled.length ? Math.round(rolled.reduce((sum, stat) => sum + stat.quality, 0) / rolled.length * 100) : 100;
  fields.base_target.value = fields.base_target_number.value = startQuality;
  form.querySelector(".roll-field").hidden = form.querySelector(".seed-line").hidden = !rolled.length;

  function enchantmentRow(entry, index) {
    const choice = choices.get(entry.template_id);
    return `<li class="enchant-row" data-index="${index}">
      <label class="enchant-name">Enchantment ${index + 1}<select data-field="template_id">${options.enchantments.map(c =>
        `<option value="${esc(c.template_id)}" ${c.template_id === entry.template_id ? "selected" : ""}>${esc(choiceLabel(c))}</option>`).join("")}</select>
        <small class="mono">${esc(entry.template_id)}${choice && !choice.compatible ? " · from this item's fixed set" : ""}</small></label>
      <label>Tier<select data-field="tier">${tierOptions(entry.kind, entry.tier)}</select></label>
      <label>Origin rarity<select data-field="kind">${rarities.map((name, value) =>
        `<option value="${value}" ${value === entry.kind ? "selected" : ""}>${esc(name)}</option>`).join("")}</select></label>
      <div class="roll-field"><label for="roll-${index}">Roll</label><div class="roll-control">
        <input id="roll-${index}" data-field="value" type="range" min="0" max="1000" step="1" value="${Math.round(entry.value * 1000)}">
        <input data-field="value_number" type="number" min="0" max="100" step="0.1" value="${(entry.value * 100).toFixed(1)}" aria-label="Enchantment ${index + 1} roll percent"><span aria-hidden="true">%</span></div></div>
      <div class="enchant-result" data-result aria-live="polite"></div>
      <button type="button" class="text-button" data-action="remove" aria-label="Remove enchantment ${index + 1}">Remove</button>
    </li>`;
  }

  function renderRows(focusIndex = null) {
    $("#enchant-list").innerHTML = draft.enchantments.map(enchantmentRow).join("") ||
      '<li class="empty">No enchantments. Add one below.</li>';
    $("#enchant-count").textContent = `${draft.enchantments.length} of ${options.max_enchantments}`;
    form.querySelector('[data-action="add"]').disabled = draft.enchantments.length >= options.max_enchantments || !options.enchantments.length;
    form.querySelectorAll("[data-result]").forEach(cell => { cell.innerHTML = '<span class="muted">Updating…</span>'; });
    if (focusIndex !== null) form.querySelector(`.enchant-row[data-index="${focusIndex}"] select`)?.focus();
  }

  function showResults(result) {
    fields.base_tier.value = String(draft.base_enchantment_tier);
    fields.base_seed.value = String(draft.base_seed);
    $("#base-stats").innerHTML = !result.base.stats.length ? '<p class="muted">This item has no base stats.</p>' :
      `<div class="table-wrap"><table><thead><tr><th scope="col">Base stat</th><th scope="col">Value</th><th scope="col">Range at this tier</th><th scope="col">Roll quality</th></tr></thead><tbody>${
      result.base.stats.map((stat, i) => {
        const before = doc.base.stats[i];
        const moved = before && before.text !== stat.text;
        return `<tr><td>${esc(stat.label)}<span class="sub">${esc(stat.name)}</span></td>
          <td><strong class="${moved ? "changed" : ""}">${esc(stat.text)}</strong>${moved ? `<span class="sub">was ${esc(before.text)}</span>` : ""}</td>
          <td>${esc(stat.range || "Fixed")}</td>
          <td>${stat.quality === null ? '<span class="muted">Fixed</span>' : qualityBar(stat.quality)}</td></tr>`;
      }).join("")}</tbody></table></div>`;
    form.querySelectorAll(".enchant-row").forEach(row => {
      const lines = result.enchantments[Number(row.dataset.index)]?.lines || [];
      row.querySelector("[data-result]").innerHTML = lines.map(line =>
        `<strong>${esc(line.text)}</strong> <span>${esc(line.label)}</span><small>${esc(line.range || "")}</small>`).join("") || '<span class="muted">—</span>';
    });
    const changes = describe();
    $("#editor-state").textContent = changes.length ? `${changes.length} unsaved change${changes.length === 1 ? "" : "s"}` : "No changes";
    form.querySelector('button[type="submit"]').disabled = !changes.length;
    form.querySelector('[data-action="reset"]').disabled = !changes.length && target === null;
  }

  function qualityBar(quality) {
    const value = Math.round(quality * 100);
    return `<span class="quality"><meter min="0" max="100" low="34" high="80" optimum="100" value="${value}"></meter>${value}%</span>`;
  }

  const preview = debounce(() => { pending = load(); }, 250);
  async function load() {
    const id = ++request;
    $("#item-editor-error").textContent = "";
    $("#editor-state").textContent = target !== null ? "Searching rolls…" : "Updating preview…";
    try {
      const query = new URLSearchParams({draft: JSON.stringify(target === null ? draft : {...draft, base_target: target})});
      const result = await api(`${path}?${query}`);
      if (id !== request || !form.isConnected) return;
      if (target !== null) { draft.base_seed = result.base.seed; target = null; }
      showResults(result);
      announce($("#editor-state").textContent);
    } catch (error) {
      if (id !== request || !form.isConnected) return;
      $("#item-editor-error").textContent = error.message;
      $("#editor-state").textContent = "Preview failed";
    }
  }
  const changed = () => { form.querySelector('button[type="submit"]').disabled = true; preview(); };

  function describe() {
    const lines = [], name = id => choices.get(id)?.name || id;
    if (draft.base_enchantment_tier !== saved.base_enchantment_tier) lines.push(`base tier ${saved.base_enchantment_tier} → ${draft.base_enchantment_tier}`);
    if (draft.base_seed !== saved.base_seed) lines.push(`base roll seed ${saved.base_seed} → ${draft.base_seed}`);
    const count = Math.max(draft.enchantments.length, saved.enchantments.length);
    for (let i = 0; i < count; i++) {
      const before = saved.enchantments[i], after = draft.enchantments[i];
      if (!after) { lines.push(`removed ${name(before.template_id)}`); continue; }
      if (!before) { lines.push(`added ${name(after.template_id)} (tier ${after.tier}, roll ${percent(after.value)})`); continue; }
      const parts = [];
      if (before.template_id !== after.template_id) parts.push(`${name(before.template_id)} → ${name(after.template_id)}`);
      if (before.tier !== after.tier) parts.push(`tier ${before.tier} → ${after.tier}`);
      if (before.kind !== after.kind) parts.push(`origin ${rarities[before.kind]} → ${rarities[after.kind]}`);
      if (before.value !== after.value) parts.push(`roll ${percent(before.value)} → ${percent(after.value)}`);
      if (parts.length) lines.push(`enchantment ${i + 1}: ${parts.join(", ")}`);
    }
    return lines;
  }

  form.addEventListener("input", event => {
    const element = event.target, row = element.closest(".enchant-row");
    if (row) {
      const entry = draft.enchantments[Number(row.dataset.index)];
      if (element.dataset.field === "value") {
        entry.value = Number(element.value) / 1000;
        row.querySelector('[data-field="value_number"]').value = (entry.value * 100).toFixed(1);
      } else if (element.dataset.field === "value_number") {
        const value = Number(element.value);
        if (element.value === "" || !Number.isFinite(value)) return;
        entry.value = Math.min(1, Math.max(0, Math.round(value * 10) / 1000));
        row.querySelector('[data-field="value"]').value = Math.round(entry.value * 1000);
      } else return;
      changed();
    } else if (element.name === "base_target" || element.name === "base_target_number") {
      const value = Math.min(100, Math.max(0, Math.round(Number(element.value))));
      if (element.value === "" || !Number.isFinite(value)) return;
      fields.base_target.value = fields.base_target_number.value = value;
      target = value / 100;
      changed();
    }
  });
  form.addEventListener("change", event => {
    const element = event.target, row = element.closest(".enchant-row");
    if (row) {
      const index = Number(row.dataset.index), entry = draft.enchantments[index];
      if (element.dataset.field === "template_id") entry.template_id = element.value;
      else if (element.dataset.field === "tier") entry.tier = Number(element.value);
      else if (element.dataset.field === "kind") {
        entry.kind = Number(element.value);
        row.querySelector('[data-field="tier"]').innerHTML = tierOptions(entry.kind, entry.tier);
      } else return;
      if (element.dataset.field === "template_id") row.querySelector("small.mono").textContent = entry.template_id;
      changed();
    } else if (element.name === "base_tier") {
      draft.base_enchantment_tier = Number(element.value);
      changed();
    } else if (element.name === "base_seed") {
      const seed = Number(element.value);
      if (!Number.isInteger(seed) || seed < 1 || seed > options.max_seed) {
        $("#item-editor-error").textContent = `The roll seed must be a whole number from 1 to ${options.max_seed}.`;
        return;
      }
      draft.base_seed = seed; target = null;
      changed();
    }
  });
  form.addEventListener("click", event => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    if (action === "remove") {
      const index = Number(button.closest(".enchant-row").dataset.index);
      draft.enchantments.splice(index, 1);
      renderRows();
      (form.querySelector(`.enchant-row[data-index="${Math.min(index, draft.enchantments.length - 1)}"] select`) || form.querySelector('[data-action="add"]')).focus();
    } else if (action === "add") {
      const used = new Set(draft.enchantments.map(entry => entry.template_id));
      const choice = options.enchantments.find(c => !used.has(c.template_id)) || options.enchantments[0];
      const tier = draft.enchantments.at(-1)?.tier ?? 0;
      draft.enchantments.push({template_id: choice.template_id, value: 1, tier, kind: Math.min(doc.item.rarity, rarities.length - 1), origin_item_level: Math.max(1, doc.item.item_level)});
      renderRows(draft.enchantments.length - 1);
    } else if (action === "max-rolls") {
      draft.enchantments.forEach(entry => { entry.value = 1; });
      renderRows();
    } else if (action === "apply-tier") {
      const tier = Number(fields.all_tiers.value);
      draft.enchantments.forEach(entry => { entry.tier = tier; });
      renderRows();
    } else if (action === "random-seed") {
      draft.base_seed = 1 + Math.floor(Math.random() * options.max_seed); target = null;
    } else if (action === "reset") {
      draft = structuredClone(saved); target = null;
      fields.base_target.value = fields.base_target_number.value = startQuality;
      renderRows();
    }
    changed();
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await pending;
      if (target !== null) { pending = load(); await pending; }
      const lines = describe();
      if (!lines.length) throw new Error("No changes to save.");
      const reason = await confirmAction("Save item stats?",
        `${doc.item.template_id} (item ${doc.item.item_id}): ${lines.join("; ")}.`, "Save item");
      if (reason === null) return;
      const result = await api(`/api/characters/${characterId}/inventory/${doc.item.item_id}/edit`,
        {changes: draft, reason});
      notice(`Saved ${result.template_id} (item ${result.item_id})${result.online ? " and sent it to the game client" : ""}.`);
      window.dispatchEvent(new Event("reload-page"));
    } catch (error) {
      $("#item-editor-error").textContent = error.message;
      $("#item-editor-error").scrollIntoView({block: "nearest"});
    } finally { button.disabled = !describe().length; }
  });
  renderRows();
  showResults(doc);
}
