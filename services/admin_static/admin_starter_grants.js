import { $, esc, human, rarities, api, table, confirmAction, notice, formError, isLegacyInterface } from "./admin_ui.js";

const CLASS_KEYS = [
  { key: "warrior", label: "Dragonknight" },
  { key: "mage", label: "Spellweaver" },
  { key: "ranger", label: "Ranger" },
  { key: "dwarf", label: "Steam Mechanicus" },
];

function rarityOptions(selectedRarity) {
  return rarities.map((r, i) =>
    `<option value="${i}" ${Number(selectedRarity) === i ? "selected" : ""}>${esc(r)}</option>`
  ).join("");
}

function slotOptions(selectedSlot, slots) {
  const customSlots = [
    { slot: "inventory", name: "Inventory Bag" },
    { slot: "ring1", name: "Ring 1 (Slot 8)" },
    { slot: "ring2", name: "Ring 2 (Slot 9)" },
  ];
  return customSlots.map(s =>
    `<option value="${s.slot}" ${selectedSlot === s.slot ? "selected" : ""}>${esc(s.name)}</option>`
  ).join("");
}

function renderSharedItemRow(item, index, slots) {
  return `
    <tr data-shared-index="${index}">
      <td>
        <strong>${esc(item.name || item.template_id)}</strong>
        <div><code class="resource">${esc(item.template_id)}</code></div>
      </td>
      <td>
        <select name="shared-slot-${index}">
          ${slotOptions(item.slot, slots)}
        </select>
      </td>
      <td>
        <input name="shared-amount-${index}" type="number" min="1" max="999999" value="${esc(item.amount || 1)}" style="width: 80px;">
      </td>
      <td>
        <input name="shared-level-${index}" type="number" min="1" max="100" value="${esc(item.item_level || 1)}" style="width: 70px;">
      </td>
      <td>
        <select name="shared-rarity-${index}">
          ${rarityOptions(item.rarity || 0)}
        </select>
      </td>
      <td>
        <button type="button" class="secondary delete-shared-btn" data-delete-index="${index}" title="Remove item">✕</button>
      </td>
    </tr>
  `;
}

function renderClassEquipmentTab(classKey, items) {
  return `
    <div class="class-eq-panel" data-class-panel="${classKey}">
      <table class="data-table">
        <thead>
          <tr>
            <th>Slot</th>
            <th>Item Template ID</th>
            <th>Level</th>
            <th>Rarity</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((item, idx) => `
            <tr>
              <td>
                <strong>${esc(item.slot_name || `Slot ${item.slot}`)}</strong>
                <input type="hidden" name="classeq-${classKey}-${idx}-slot" value="${esc(item.slot)}">
              </td>
              <td>
                <input name="classeq-${classKey}-${idx}-template" type="text" value="${esc(item.template_id)}" required style="width: 100%;">
              </td>
              <td>
                <input name="classeq-${classKey}-${idx}-level" type="number" min="1" max="100" value="${esc(item.item_level || 1)}" style="width: 70px;">
              </td>
              <td>
                <select name="classeq-${classKey}-${idx}-rarity">
                  ${rarityOptions(item.rarity || 0)}
                </select>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderContent(data) {
  const config = data.config;
  const m = config.membership || {};
  const c = config.currencies || {};
  const gc = c.game_currencies || {};
  const sharedItems = config.starter_items?.shared || [];
  const classEquipment = config.starter_items?.class_equipment || {};

  return `
    <form id="starter-grants-form" class="form-page">
      <!-- Section 1: Membership (Pre-Deluxe) -->
      <fieldset class="form-section">
        <legend>Pre-Deluxe & Membership</legend>
        <p class="help">Configure the default account membership given to newly created accounts and characters. Deluxe includes all Premium features plus exclusive rewards.</p>
        <div class="form-grid">
          <label>Membership tier
            <select name="membership-type">
              ${data.membership_options.map(opt =>
                `<option value="${opt.value}" ${Number(m.type) === opt.value ? "selected" : ""}>${esc(opt.label)}</option>`
              ).join("")}
            </select>
          </label>
          <label>Duration (days)
            <input name="membership-duration" type="number" min="0" max="36500" value="${esc(m.duration_days ?? 0)}" required>
            <small>Enter total days of active membership (0 for none).</small>
          </label>
          <label class="check" style="align-self: center; margin-top: 1.5rem;">
            <input type="checkbox" name="membership-active" ${m.active ? "checked" : ""}>
            Membership Active immediately
          </label>
        </div>
      </fieldset>

      <!-- Section 2: Core Currencies -->
      <fieldset class="form-section">
        <legend>Core Currencies</legend>
        <p class="help">Starting balances granted to each new character on first world entry.</p>
        <div class="form-grid">
          <label>Andermant
            <input name="curr-andermant" type="number" min="0" max="2000000000" value="${esc(c.andermant ?? 100000000)}" required>
            <small>Premium cash currency.</small>
          </label>
          <label>Gold
            <input name="curr-gold" type="number" min="0" max="2000000000" value="${esc(c.gold ?? 100000000)}" required>
            <small>1 Gold = 100 Silver = 10,000 Copper.</small>
          </label>
          <label>Silver
            <input name="curr-silver" type="number" min="0" max="99" value="${esc(c.silver ?? 0)}" required>
          </label>
          <label>Copper
            <input name="curr-copper" type="number" min="0" max="99" value="${esc(c.copper ?? 0)}" required>
          </label>
          <label>Badges of Honor
            <input name="curr-badge_of_honor" type="number" min="0" max="2000000000" value="${esc(c.badge_of_honor ?? 8883102)}" required>
            <small>PvP combat currency.</small>
          </label>
          <label>Draken
            <input name="curr-draken" type="number" min="0" max="2000000000" value="${esc(c.draken ?? 30424)}" required>
            <small>Event and festival trader currency.</small>
          </label>
          <label>Glyphs of Power
            <input name="curr-glyph_of_power" type="number" min="0" max="2000000000" value="${esc(c.glyph_of_power ?? 710712427)}" required>
            <small>Equipment item level upgrading currency.</small>
          </label>
        </div>
      </fieldset>

      <!-- Section 3: Extended Wallet Currencies -->
      <fieldset class="form-section">
        <legend>Extended Game Wallet Currencies</legend>
        <p class="help">Special resource balances stored in the character's game-currency wallet.</p>
        <div class="form-grid">
          ${data.currency_definitions.map(curr => `
            <label>${esc(curr.label)}
              <input name="gc-${esc(curr.name)}" type="number" min="0" max="2000000000" value="${esc(gc[curr.name] ?? curr.default_balance)}" required>
              <small><code class="resource">${esc(curr.wire_id)}</code></small>
            </label>
          `).join("")}
        </div>
      </fieldset>

      <!-- Section 4: Shared Starter Items -->
      <fieldset class="form-section">
        <legend>Shared Starter Items & Equipment</legend>
        <p class="help">Items given to all classes: equipped rings, mount, pet, starter consumables, season passes and stones.</p>

        <table class="data-table" id="shared-items-table">
          <thead>
            <tr>
              <th>Item / Template</th>
              <th>Destination Slot</th>
              <th>Amount</th>
              <th>Level</th>
              <th>Rarity</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="shared-items-body">
            ${sharedItems.map((item, idx) => renderSharedItemRow(item, idx, data.slots)).join("")}
          </tbody>
        </table>

        <!-- Item Search / Quick Add Area -->
        <div class="panel" style="margin-top: 1rem; padding: 1rem; border: 1px solid var(--border-subtle, #333); border-radius: 4px;">
          <h3>Add Item to Starter Grants</h3>
          <p class="muted">Search for an item template or enter a template ID directly to add it to the starter items list.</p>
          <div class="toolbar" style="margin-bottom: 0.5rem;">
            <input id="item-search-input" placeholder="Search item by name or template ID..." maxlength="100" style="flex: 1;">
            <button id="item-search-btn" type="button" class="secondary">Search</button>
          </div>
          <div id="item-search-results" class="picker-results"></div>
          <div id="item-manual-add" class="form-grid" style="margin-top: 0.75rem;">
            <label>Template ID
              <input id="new-item-template" placeholder="e.g. all_mythic_ring_death">
            </label>
            <label>Name (optional)
              <input id="new-item-name" placeholder="Item display name">
            </label>
            <label>Slot
              <select id="new-item-slot">
                <option value="inventory">Inventory Bag</option>
                <option value="ring1">Ring 1 (Slot 8)</option>
                <option value="ring2">Ring 2 (Slot 9)</option>
              </select>
            </label>
            <label>Amount
              <input id="new-item-amount" type="number" min="1" max="999999" value="1">
            </label>
            <label>Level
              <input id="new-item-level" type="number" min="1" max="100" value="1">
            </label>
            <label>Rarity
              <select id="new-item-rarity">
                ${rarityOptions(0)}
              </select>
            </label>
          </div>
          <button id="add-item-btn" type="button" class="secondary" style="margin-top: 0.5rem;">+ Add To Starter Items</button>
        </div>
      </fieldset>

      <!-- Section 5: Class-Specific Core Equipment -->
      <fieldset class="form-section">
        <legend>Class-Specific Core Equipment</legend>
        <p class="help">The standard worn gear slots (Helmet, Shoulders, Torso, Weapon, Shield/Offhand, etc.) for each character class.</p>
        <div class="tabs-nav" style="display: flex; gap: 0.5rem; margin-bottom: 1rem;">
          ${CLASS_KEYS.map((cls, idx) => `
            <button type="button" class="tab-button ${idx === 0 ? "active" : ""}" data-class-tab="${cls.key}">${esc(cls.label)}</button>
          `).join("")}
        </div>
        <div id="class-equipment-container">
          ${CLASS_KEYS.map(cls => renderClassEquipmentTab(cls.key, classEquipment[cls.key] || [])).join("")}
        </div>
      </fieldset>

      <p id="form-error" class="error-text" role="alert"></p>

      <div class="form-actions" style="display: flex; justify-content: space-between; align-items: center; margin-top: 2rem;">
        <button id="reset-starter-btn" type="button" class="secondary danger">Reset to Defaults</button>
        <button type="submit">Save Starter Settings</button>
      </div>
    </form>
  `;
}

export async function starterGrantsPage(route) {
  const data = await api("/api/starter-grants");
  let sharedItemsState = [...(data.config.starter_items?.shared || [])];

  const result = {
    title: "Starter grants",
    description: "Configure starting membership (Pre-Deluxe), currencies, and initial items for new accounts and characters.",
    breadcrumbs: [["Economy & rewards", "bookings"], ["Starter grants"]],
    html: renderContent(data),
    mount() {
      bindEvents();
    },
  };

  function bindEvents() {
    const form = $("#starter-grants-form");
    if (!form) return;

    // Switch class equipment tabs
    const tabButtons = form.querySelectorAll("[data-class-tab]");
    const classPanels = form.querySelectorAll("[data-class-panel]");

    function activateClassTab(key) {
      tabButtons.forEach(btn => {
        btn.classList.toggle("active", btn.dataset.classTab === key);
      });
      classPanels.forEach(panel => {
        panel.hidden = panel.dataset.classPanel !== key;
      });
    }
    activateClassTab("warrior");

    tabButtons.forEach(btn => {
      btn.addEventListener("click", () => activateClassTab(btn.dataset.classTab));
    });

    // Handle delete shared item
    function bindDeleteButtons() {
      form.querySelectorAll(".delete-shared-btn").forEach(btn => {
        btn.onclick = () => {
          const idx = Number(btn.dataset.deleteIndex);
          sharedItemsState.splice(idx, 1);
          refreshSharedTable();
        };
      });
    }

    function refreshSharedTable() {
      const tbody = $("#shared-items-body");
      if (!tbody) return;
      tbody.innerHTML = sharedItemsState.map((item, idx) =>
        renderSharedItemRow(item, idx, data.slots)
      ).join("");
      bindDeleteButtons();
    }
    bindDeleteButtons();

    // Item search functionality
    const searchInput = $("#item-search-input");
    const searchBtn = $("#item-search-btn");
    const searchResults = $("#item-search-results");

    async function executeSearch() {
      const q = (searchInput.value || "").trim();
      if (!q) return;
      searchBtn.disabled = true;
      searchResults.innerHTML = '<p class="muted">Searching catalog...</p>';
      try {
        const res = await api(`/api/items?limit=15&q=${encodeURIComponent(q)}`);
        if (!res.rows || res.rows.length === 0) {
          searchResults.innerHTML = '<p class="muted">No items matched your query.</p>';
          return;
        }
        searchResults.innerHTML = table(
          ["Name", "Template ID", "Category", "Rarity", ""],
          res.rows.map(item => [
            esc(item.name),
            `<code class="resource">${esc(item.template_id)}</code>`,
            esc(item.category || "—"),
            esc(item.rarity || "Common"),
            `<button type="button" class="secondary select-search-item" data-template="${esc(item.template_id)}" data-name="${esc(item.name)}" data-rarity="${esc(item.rarity_value || 0)}">Select</button>`,
          ])
        );

        searchResults.querySelectorAll(".select-search-item").forEach(b => {
          b.onclick = () => {
            $("#new-item-template").value = b.dataset.template;
            $("#new-item-name").value = b.dataset.name;
            $("#new-item-rarity").value = b.dataset.rarity || "0";
            searchResults.innerHTML = "";
            searchInput.value = "";
          };
        });
      } catch (err) {
        formError(err);
      } finally {
        searchBtn.disabled = false;
      }
    }

    searchBtn.addEventListener("click", executeSearch);
    searchInput.addEventListener("keydown", e => {
      if (e.key === "Enter") {
        e.preventDefault();
        executeSearch();
      }
    });

    // Add new item button
    const addItemBtn = $("#add-item-btn");
    addItemBtn.addEventListener("click", () => {
      const templateId = ($("#new-item-template").value || "").trim();
      if (!templateId) {
        alert("Please enter a template ID.");
        return;
      }
      const name = ($("#new-item-name").value || "").trim() || templateId;
      const slot = $("#new-item-slot").value;
      const amount = Math.max(1, Number($("#new-item-amount").value) || 1);
      const level = Math.max(1, Number($("#new-item-level").value) || 1);
      const rarity = Number($("#new-item-rarity").value) || 0;

      sharedItemsState.push({
        template_id: templateId,
        name: name,
        slot: slot,
        amount: amount,
        item_level: level,
        rarity: rarity,
      });

      $("#new-item-template").value = "";
      $("#new-item-name").value = "";
      $("#new-item-amount").value = "1";
      $("#new-item-level").value = "1";
      refreshSharedTable();
    });

    // Reset to defaults
    const resetBtn = $("#reset-starter-btn");
    resetBtn.addEventListener("click", async () => {
      try {
        const reason = await confirmAction(
          "Reset Starter Grants",
          "Are you sure you want to restore all starter settings (Pre-Deluxe, currencies and equipment) to their default values?",
          "Reset to defaults"
        );
        if (reason === null) return;
        resetBtn.disabled = true;
        const resp = await api("/api/starter-grants/reset", {
          revision: data.revision,
          reason,
        });
        notice("Starter configuration has been reset to defaults.");
        location.reload();
      } catch (err) {
        formError(err);
      } finally {
        resetBtn.disabled = false;
      }
    });

    // Form submit
    form.addEventListener("submit", async event => {
      event.preventDefault();
      $("#form-error").textContent = "";

      // Gather updated shared items from table inputs
      const finalShared = [];
      const trs = form.querySelectorAll("#shared-items-body tr");
      trs.forEach((tr, idx) => {
        if (idx < sharedItemsState.length) {
          const base = sharedItemsState[idx];
          const slot = tr.querySelector(`[name="shared-slot-${idx}"]`)?.value || base.slot;
          const amount = Number(tr.querySelector(`[name="shared-amount-${idx}"]`)?.value) || base.amount;
          const level = Number(tr.querySelector(`[name="shared-level-${idx}"]`)?.value) || base.item_level;
          const rarity = Number(tr.querySelector(`[name="shared-rarity-${idx}"]`)?.value) || base.rarity;
          finalShared.push({
            template_id: base.template_id,
            name: base.name,
            slot: slot,
            amount: amount,
            item_level: level,
            rarity: rarity,
          });
        }
      });

      // Gather class equipment
      const finalClassEq = {};
      for (const cls of CLASS_KEYS) {
        const classItems = [];
        const panel = form.querySelector(`[data-class-panel="${cls.key}"]`);
        if (panel) {
          const rows = panel.querySelectorAll("tbody tr");
          rows.forEach((row, idx) => {
            const slot = Number(row.querySelector(`[name="classeq-${cls.key}-${idx}-slot"]`)?.value || 0);
            const template = (row.querySelector(`[name="classeq-${cls.key}-${idx}-template"]`)?.value || "").trim();
            const level = Number(row.querySelector(`[name="classeq-${cls.key}-${idx}-level"]`)?.value || 1);
            const rarity = Number(row.querySelector(`[name="classeq-${cls.key}-${idx}-rarity"]`)?.value || 0);
            if (template) {
              classItems.push({
                slot,
                template_id: template,
                item_level: level,
                rarity,
              });
            }
          });
        }
        finalClassEq[cls.key] = classItems;
      }

      // Gather game currencies
      const finalGc = {};
      data.currency_definitions.forEach(curr => {
        const val = Number(form.elements[`gc-${curr.name}`]?.value);
        if (!isNaN(val)) finalGc[curr.name] = val;
      });

      const payloadConfig = {
        membership: {
          type: Number(form.elements["membership-type"].value),
          duration_days: Number(form.elements["membership-duration"].value),
          active: form.elements["membership-active"].checked,
        },
        currencies: {
          andermant: Number(form.elements["curr-andermant"].value),
          gold: Number(form.elements["curr-gold"].value),
          silver: Number(form.elements["curr-silver"].value),
          copper: Number(form.elements["curr-copper"].value),
          badge_of_honor: Number(form.elements["curr-badge_of_honor"].value),
          draken: Number(form.elements["curr-draken"].value),
          glyph_of_power: Number(form.elements["curr-glyph_of_power"].value),
          game_currencies: finalGc,
        },
        starter_items: {
          shared: finalShared,
          class_equipment: finalClassEq,
        },
      };

      try {
        const reason = await confirmAction(
          "Save Starter Settings",
          "Save changes to starter membership, currency and equipment configuration? Changes apply to all newly created accounts and characters immediately.",
          "Save settings"
        );
        if (reason === null) return;
        const submitBtn = form.querySelector('button[type="submit"]');
        submitBtn.disabled = true;

        const response = await api("/api/starter-grants", {
          revision: data.revision,
          reason,
          config: payloadConfig,
        });

        notice("Starter grants saved successfully.");
        data.revision = response.revision;
        data.config = response.config;
      } catch (err) {
        formError(err);
      } finally {
        const submitBtn = form.querySelector('button[type="submit"]');
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }

  return result;
}
