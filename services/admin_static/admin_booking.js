import { $, esc, human, classes, rarities, api, link, navigate, table, facts, section, formError, notice, confirmAction } from "./admin_ui.js";

const requestId = () => Array.from(crypto.getRandomValues(new Uint8Array(24)), v => v.toString(16).padStart(2, "0")).join("");

export async function newBookingPage(route) {
  const currencies = (await api("/api/currencies")).currencies;
  let account = null, character = null, item = null;
  if (route.params.account_id) account = await api(`/api/accounts/${route.params.account_id}`);
  if (route.params.character_id) {
    const detail = await api(`/api/characters/${route.params.character_id}`);
    if (!detail.account) throw new Error("This character is not tied to an account.");
    if (account && account.account.account_id !== detail.account.account_id) throw new Error("Character and account do not match.");
    if (!account) account = await api(`/api/accounts/${detail.account.account_id}`);
    character = account.characters.find(c => c.character_id === detail.profile.character_id);
  } else if (account?.characters.length === 1) character = account.characters[0];
  if (route.params.template) item = await api(`/api/items/${encodeURIComponent(route.params.template)}`);
  const result = {
    title: "New booking", description: "Choose a recipient, configure the grant, then review it before booking.",
    breadcrumbs: account ? [["Accounts", "accounts"], [account.account.username, `accounts/${account.account.account_id}`],
      ...(character ? [[character.name, `characters/${character.character_id}`]] : []), ["New booking"]] : [["Bookings", "bookings"], ["New booking"]],
    html: `<div class="form-page"><form id="booking-form">
      <fieldset class="form-section"><legend>1. Recipient</legend>
        <div id="selected-account"></div>
        <div id="account-picker"><label>Find account</label><div class="toolbar"><input id="account-search" aria-label="Find account" placeholder="Username or account ID" maxlength="100"><button id="find-account" type="button" class="secondary">Find account</button></div><div id="account-results" class="picker-results"></div></div>
        <label>Character<select name="character_id" required><option value="">Choose an account first</option></select></label>
        <p id="character-note" class="help"></p>
      </fieldset>
      <fieldset class="form-section"><legend>2. Grant</legend>
        <div class="form-grid"><label>Grant type<select name="kind"><option value="currency">Currency</option><option value="item" ${item ? "selected" : ""}>Item</option></select></label>
          <label id="currency-field">Currency<select name="currency">${currencies.map(c => `<option value="${c}">${esc(human(c))}</option>`).join("")}</select></label></div>
        <div id="item-picker" hidden><div id="selected-item"></div><div id="item-search-area"><label>Find an item by its in-game name</label><div class="toolbar"><input id="item-search" aria-label="Find item" placeholder="In-game name, category, or template ID" maxlength="100"><button id="find-item" type="button" class="secondary">Find item</button></div><div id="item-results" class="picker-results"></div></div></div>
        <div id="grant-options"></div>
      </fieldset>
      <p class="help">Online characters receive the grant immediately with an in-game booking notification. Offline characters receive it on their next game entry. Every booking is audited; you enter the reason when you create it.</p>
      <p id="form-error" class="error-text" role="alert"></p>
      <div class="form-actions">${link("Cancel", account ? `accounts/${account.account.account_id}` : "bookings", {}, "left")}<button type="submit">Review booking</button></div>
    </form><section id="booking-review" hidden></section></div>`
  };
  result.mount = () => {
    const form = $("#booking-form");
    let prepared = null, recipientGeneration = 0, itemGeneration = 0;
    const alive = () => form.isConnected;
    const field = name => form.elements.namedItem(name);
    function drawAccount() {
      $("#selected-account").innerHTML = account ? `<div class="selection"><div><strong>${esc(account.account.username)}</strong><small>Account ${account.account.account_id}</small></div><button id="change-account" type="button" class="secondary">Change</button></div>` : "";
      $("#account-picker").hidden = !!account;
      $("#change-account")?.addEventListener("click", () => {
        recipientGeneration++; account = null; character = null; drawAccount();
      });
      field("character_id").innerHTML = '<option value="">Select a character</option>' + (account?.characters || []).map(c =>
        `<option value="${c.character_id}" ${c.character_id === character?.character_id ? "selected" : ""}>${esc(c.name)} · ${esc(classes[c.class_id] || c.class_id)} · Level ${c.level} · ${c.character_id}${c.has_state ? "" : " (no saved state)"}</option>`).join("");
      field("character_id").disabled = !account;
      drawCharacterNote();
    }
    function drawCharacterNote() {
      $("#character-note").textContent = character ? (!character.has_state ? "This character must enter the game once before it can receive bookings." :
        character.online ? "Currently online. Delivery will be attempted immediately." : "Offline. Delivery waits for the next login.") : "";
    }
    async function findAccount() {
      const version = ++recipientGeneration, button = $("#find-account"); button.disabled = true;
      try {
        const data = await api(`/api/accounts?limit=20&q=${encodeURIComponent($("#account-search").value)}`);
        if (!alive() || version !== recipientGeneration) return;
        $("#account-results").innerHTML = table(["Account", "ID", "Characters", ""], data.rows.map(a => [esc(a.username), a.account_id, a.character_ids.length,
          `<button type="button" class="secondary" data-account="${a.account_id}">Select</button>`]));
        for (const button of $("#account-results").querySelectorAll("[data-account]")) button.addEventListener("click", async () => {
          const selectionVersion = ++recipientGeneration; button.disabled = true;
          try {
            const selected = await api(`/api/accounts/${button.dataset.account}`);
            if (!alive() || selectionVersion !== recipientGeneration) return;
            account = selected; character = selected.characters.length === 1 ? selected.characters[0] : null;
            drawAccount(); drawOptions();
          } catch (error) { if (alive()) formError(error); } finally { button.disabled = false; }
        });
        if (data.total > data.rows.length) $("#account-results").insertAdjacentHTML("beforeend", '<p class="help">Refine the search to find more accounts.</p>');
      } catch (error) { if (alive()) formError(error); } finally { button.disabled = false; }
    }
    function drawItem() {
      $("#selected-item").innerHTML = item ? `<div class="selection"><div><strong>${esc(item.name)}</strong><small class="mono">${esc(item.template_id)}</small> · <small>${esc(item.category)} · ${esc(item.rarity_name)} · ${item.equipment ? "Equipment" : "Stack limit " + item.max_stack_size}</small></div><button id="change-item" type="button" class="secondary">Change</button></div>` : "";
      $("#item-search-area").hidden = !!item;
      $("#change-item")?.addEventListener("click", () => { itemGeneration++; item = null; drawItem(); drawOptions(); });
    }
    async function findItem() {
      const version = ++itemGeneration, button = $("#find-item"); button.disabled = true;
      try {
        const data = await api(`/api/items?limit=30&q=${encodeURIComponent($("#item-search").value)}`);
        if (!alive() || version !== itemGeneration) return;
        $("#item-results").innerHTML = table(["Name", "Template", "Category", "Rarity", ""], data.rows.map(i => [
          esc(i.name), `<code class="resource">${esc(i.template_id)}</code>`, esc(i.category), esc(i.rarity),
          `<button type="button" class="secondary" data-template="${esc(i.template_id)}">Select</button>`
        ]));
        for (const button of $("#item-results").querySelectorAll("[data-template]")) button.addEventListener("click", async () => {
          const selectionVersion = ++itemGeneration; button.disabled = true;
          try {
            const selected = await api(`/api/items/${encodeURIComponent(button.dataset.template)}`);
            if (!alive() || selectionVersion !== itemGeneration) return;
            item = selected; drawItem(); drawOptions();
          } catch (error) { if (alive()) formError(error); } finally { button.disabled = false; }
        });
        if (data.total > data.rows.length) $("#item-results").insertAdjacentHTML("beforeend", '<p class="help">Refine the search to find more items.</p>');
      } catch (error) { if (alive()) formError(error); } finally { button.disabled = false; }
    }
    function drawOptions() {
      const isItem = field("kind").value === "item", target = $("#grant-options");
      $("#item-picker").hidden = !isItem; $("#currency-field").hidden = isItem;
      if (isItem && !item) { target.innerHTML = '<p class="help">Select a template to see its booking options.</p>'; return; }
      if (isItem && !item.bookable) { target.innerHTML = '<p class="error-text">This template cannot be booked as an inventory item. Choose another template or use a currency booking.</p>'; return; }
      if (!isItem || !item.equipment) {
        target.innerHTML = `<div class="form-grid"><label>${isItem ? "Stack size" : "Amount"}<input name="amount" type="number" min="1" max="${isItem ? item.max_stack_size : 1000000000}" step="1" value="1" required><small>${isItem ? `One stack, up to ${item.max_stack_size} units.` : "Enter a positive amount in the game's base currency unit."}</small></label></div>`;
        return;
      }
      const level = Math.min(item.max_level, Math.max(item.min_level, character?.level || item.min_level));
      target.innerHTML = `<p class="help">One equipment item per booking. Unique, set and mythic templates keep their authored rarity.</p><div class="form-grid">
        <label>Item level<input name="item_level" type="number" min="${item.min_level}" max="${item.max_level}" step="1" value="${level}" required></label>
        <label>Rarity<select name="rarity">${item.rarities.map(r => `<option value="${r.value}" ${r.value === item.rarity ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></label>
        <label>Open sockets<input name="socket_count" type="number" min="0" step="1" value="0" required><small id="socket-limit"></small></label>
        <label>Enchantment tier<select name="enchantment_tier">${item.enchantment_tiers.map(t => `<option value="${t}">${t === 0 ? "0 · Normal" : t}</option>`).join("")}</select></label>
        <label>Enchantment roll quality<select name="quality_mode"><option value="random">Random rolls</option><option value="fixed">Fixed percentage</option></select><small>Compatible enchantments are generated from the item's template, class, level and rarity.</small></label>
        <label id="quality-field" hidden>Roll percentage<input name="roll_quality" type="number" min="0" max="100" step="1" value="100" disabled><small>Applies to each generated enchantment.</small></label>
      </div>`;
      const syncSockets = () => {
        const max = item.socket_limits[Number(field("item_level").value)] ?? 0;
        field("socket_count").max = max;
        $("#socket-limit").textContent = `Maximum ${max} at this item level.`;
      };
      field("item_level").addEventListener("input", syncSockets); syncSockets();
      field("quality_mode").addEventListener("change", () => {
        const fixed = field("quality_mode").value === "fixed";
        $("#quality-field").hidden = !fixed; field("roll_quality").disabled = !fixed; field("roll_quality").required = fixed;
      });
    }
    $("#find-account").addEventListener("click", findAccount);
    $("#find-item").addEventListener("click", findItem);
    for (const [selector, action] of [["#account-search", findAccount], ["#item-search", findItem]]) {
      $(selector).addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); action(); } });
    }
    field("character_id").addEventListener("change", () => {
      character = account?.characters.find(c => String(c.character_id) === field("character_id").value) || null;
      drawCharacterNote(); drawOptions();
    });
    field("kind").addEventListener("change", drawOptions);
    form.addEventListener("submit", event => {
      event.preventDefault();
      try {
        if (!account || !character) throw new Error("Select an account and one of its characters.");
        if (!character.has_state) throw new Error("This character must enter the game once before receiving bookings.");
        const values = new FormData(form), kind = values.get("kind"), equipment = kind === "item" && item?.equipment;
        if (kind === "item" && !item?.bookable) throw new Error("Select a bookable item template.");
        const options = equipment ? {item_level: Number(values.get("item_level")), rarity: Number(values.get("rarity")),
          socket_count: Number(values.get("socket_count")), enchantment_tier: Number(values.get("enchantment_tier")),
          roll_quality: values.get("quality_mode") === "fixed" ? Number(values.get("roll_quality")) : null} : {};
        prepared = {account_id: account.account.account_id, character_id: character.character_id, kind,
          resource: kind === "item" ? item.template_id : values.get("currency"), amount: equipment ? 1 : Number(values.get("amount")),
          options, request_id: requestId()};
        const rows = [["Account", esc(account.account.username)], ["Character", `${esc(character.name)} · ${character.character_id}`],
          ["Grant", esc(kind === "item" ? item.template_id : human(prepared.resource))], [equipment ? "Quantity" : kind === "item" ? "Stack size" : "Amount", prepared.amount]];
        if (equipment) rows.push(["Item level", options.item_level], ["Rarity", esc(rarities[options.rarity])], ["Sockets", options.socket_count],
          ["Enchantment tier", options.enchantment_tier], ["Roll quality", options.roll_quality == null ? "Random" : options.roll_quality + "%"]);
        $("#booking-review").innerHTML = section("Review booking", facts(rows)) +
          '<p class="muted">This grant is delivered immediately when the character is ready online, or queued for their next game entry.</p><p id="review-error" class="error-text" role="alert"></p><div class="form-actions"><button id="edit-booking" class="secondary">Back to editing</button><button id="queue-booking">Create booking</button></div>';
        form.hidden = true; $("#booking-review").hidden = false;
        $("#edit-booking").addEventListener("click", () => { form.hidden = false; $("#booking-review").hidden = true; });
        $("#queue-booking").addEventListener("click", async event => {
          const button = event.target; button.disabled = true; $("#edit-booking").disabled = true;
          try {
            const reason = await confirmAction("Create this booking?", `Grant ${prepared.kind === "item" ? item.template_id : human(prepared.resource)} × ${prepared.amount} to ${character.name}.`, "Create booking");
            if (reason === null) return;
            const result = await api("/api/bookings", {...prepared, reason});
            notice(result.status === "delivered" ? "Booking delivered to the online character." : result.error ? "Booking saved. Review the delivery issue below." : "Booking queued for the character's next game entry."); navigate(`bookings/${result.id}`);
          } catch (error) { if (alive()) $("#review-error").textContent = error.message; }
          finally { button.disabled = false; if (alive()) $("#edit-booking").disabled = false; }
        });
        $("#booking-review").scrollIntoView({block: "start"});
      } catch (error) { formError(error); }
    });
    drawAccount(); drawItem(); drawOptions();
  };
  return result;
}

export async function batchBookingPage(route) {
  const currencies = (await api("/api/currencies")).currencies;
  let account = null, character = null;
  if (route.params.account_id) account = await api(`/api/accounts/${route.params.account_id}`);
  if (route.params.character_id) {
    const detail = await api(`/api/characters/${route.params.character_id}`);
    if (!detail.account) throw new Error("This character is not tied to an account.");
    if (account && account.account.account_id !== detail.account.account_id) throw new Error("Character and account do not match.");
    if (!account) account = await api(`/api/accounts/${detail.account.account_id}`);
    character = account.characters.find(c => c.character_id === detail.profile.character_id);
  } else if (account?.characters.length === 1) character = account.characters[0];

  const result = {
    title: "Batch booking", description: "Add multiple items or currencies to one delivery.",
    breadcrumbs: account ? [["Accounts", "accounts"], [account.account.username, `accounts/${account.account.account_id}`],
      ...(character ? [[character.name, `characters/${character.character_id}`]] : []), ["Batch booking"]] : [["Bookings", "bookings"], ["Batch booking"]],
    html: `<div class="form-page"><form id="batch-form">
      <fieldset class="form-section"><legend>1. Recipient</legend>
        <div id="selected-account"></div>
        <div id="account-picker"><label>Find account</label><div class="toolbar"><input id="account-search" aria-label="Find account" placeholder="Username or account ID" maxlength="100"><button id="find-account" type="button" class="secondary">Find account</button></div><div id="account-results" class="picker-results"></div></div>
        <label>Character<select name="character_id" required><option value="">Choose an account first</option></select></label>
        <p id="character-note" class="help"></p>
      </fieldset>
      <fieldset class="form-section"><legend>2. Items & Currencies</legend>
        <div id="batch-items"></div>
        <button type="button" id="add-item" class="secondary">+ Add item</button>
        <button type="button" id="add-currency" class="secondary">+ Add currency</button>
        <div id="batch-item-picker" class="form-section" hidden>
          <div id="batch-selected-item"></div>
          <div id="batch-item-search-area"><label for="batch-item-search">Find an item by its in-game name</label><div class="toolbar"><input id="batch-item-search" aria-label="Find item" placeholder="In-game name, category, or template ID" maxlength="100"><button id="find-batch-item" type="button" class="secondary">Find item</button></div><div id="batch-item-results" class="picker-results"></div></div>
          <div id="batch-item-options"></div>
          <p id="batch-item-error" class="error-text" role="alert"></p>
          <div class="form-actions"><button type="button" id="cancel-batch-item" class="secondary">Cancel</button><button type="button" id="confirm-batch-item" disabled>Add selected item</button></div>
        </div>
        <div id="batch-currency-picker" class="form-section" hidden>
          <div class="form-grid">
            <label>Currency<select id="batch-currency">${currencies.map(c => `<option value="${esc(c)}">${esc(human(c))}</option>`).join("")}</select></label>
            <label>Amount<input id="batch-currency-amount" type="number" min="1" max="1000000000" step="1" value="100" required><small>Enter a positive amount in the game's base currency unit.</small></label>
          </div>
          <p id="batch-currency-error" class="error-text" role="alert"></p>
          <div class="form-actions"><button type="button" id="cancel-batch-currency" class="secondary">Cancel</button><button type="button" id="confirm-batch-currency">Add currency</button></div>
        </div>
      </fieldset>
      <p class="help">All items are delivered together when the character is ready online, or queued for their next game entry. You enter the audit reason when you create the booking.</p>
      <p id="form-error" class="error-text" role="alert"></p>
      <div class="form-actions">${link("Cancel", account ? `accounts/${account.account.account_id}` : "bookings", {}, "left")}<button type="submit">Review batch booking</button></div>
    </form><section id="batch-review" hidden></section></div>`
  };
  result.mount = () => {
    const form = $("#batch-form");
    let prepared = null, recipientGeneration = 0, itemGeneration = 0, pendingItem = null;
    const alive = () => form.isConnected;
    const field = name => form.elements.namedItem(name);
    const batchItems = []; // Array of {kind, resource, amount, options, template_name}

    function drawAccount() {
      $("#selected-account").innerHTML = account ? `<div class="selection"><div><strong>${esc(account.account.username)}</strong><small>Account ${account.account.account_id}</small></div><button id="change-account" type="button" class="secondary">Change</button></div>` : "";
      $("#account-picker").hidden = !!account;
      $("#change-account")?.addEventListener("click", () => {
        recipientGeneration++; account = null; character = null; drawAccount();
      });
      field("character_id").innerHTML = '<option value="">Select a character</option>' + (account?.characters || []).map(c =>
        `<option value="${c.character_id}" ${c.character_id === character?.character_id ? "selected" : ""}>${esc(c.name)} · ${esc(classes[c.class_id] || c.class_id)} · Level ${c.level} · ${c.character_id}${c.has_state ? "" : " (no saved state)"}</option>`).join("");
      field("character_id").disabled = !account;
      drawCharacterNote();
    }
    function drawCharacterNote() {
      $("#character-note").textContent = character ? (!character.has_state ? "This character must enter the game once before it can receive bookings." :
        character.online ? "Currently online. Delivery will be attempted immediately." : "Offline. Delivery waits for the next login.") : "";
    }
    async function findAccount() {
      const version = ++recipientGeneration, button = $("#find-account"); button.disabled = true;
      try {
        const data = await api(`/api/accounts?limit=20&q=${encodeURIComponent($("#account-search").value)}`);
        if (!alive() || version !== recipientGeneration) return;
        $("#account-results").innerHTML = table(["Account", "ID", "Characters", ""], data.rows.map(a => [esc(a.username), a.account_id, a.character_ids.length,
          `<button type="button" class="secondary" data-account="${a.account_id}">Select</button>`]));
        for (const button of $("#account-results").querySelectorAll("[data-account]")) button.addEventListener("click", async () => {
          const selectionVersion = ++recipientGeneration; button.disabled = true;
          try {
            const selected = await api(`/api/accounts/${button.dataset.account}`);
            if (!alive() || selectionVersion !== recipientGeneration) return;
            account = selected; character = selected.characters.length === 1 ? selected.characters[0] : null;
            drawAccount();
          } catch (error) { if (alive()) formError(error); } finally { button.disabled = false; }
        });
        if (data.total > data.rows.length) $("#account-results").insertAdjacentHTML("beforeend", '<p class="help">Refine the search to find more accounts.</p>');
      } catch (error) { if (alive()) formError(error); } finally { button.disabled = false; }
    }
    function renderBatchItems() {
      if (!batchItems.length) {
        $("#batch-items").innerHTML = '<p class="muted">No items added yet. Use the buttons below to add items or currencies.</p>';
        return;
      }
      const rows = batchItems.map((item, idx) => [
        `<button type="button" class="secondary remove-item" data-idx="${idx}">Remove</button>`,
        esc(item.kind === "currency" ? human(item.resource) : item.template_name || item.resource),
        item.kind === "currency" ? esc(human(item.resource)) : "",
        item.amount,
        item.options ? `Rarity: ${esc(rarities[item.options.rarity])}, Level: ${item.options.item_level}` : ""
      ]);
      $("#batch-items").innerHTML = table(["", "Name", "Type", "Amount", "Options"], rows);
      for (const btn of $(".remove-item", true)) btn.addEventListener("click", () => {
        batchItems.splice(Number(btn.dataset.idx), 1);
        renderBatchItems();
      });
    }
    async function addItemEntry(kind) {
      if (kind === "item") {
        itemGeneration++;
        pendingItem = null;
        $("#batch-currency-picker").hidden = true;
        $("#batch-item-search").value = "";
        $("#batch-item-results").innerHTML = "";
        $("#batch-item-error").textContent = "";
        $("#batch-item-picker").hidden = false;
        drawBatchItemSelection();
        $("#batch-item-search").focus();
      } else {
        itemGeneration++;
        pendingItem = null;
        $("#batch-item-picker").hidden = true;
        $("#batch-currency-error").textContent = "";
        $("#batch-currency-amount").value = "100";
        $("#batch-currency-picker").hidden = false;
        $("#batch-currency").focus();
      }
    }

    function confirmBatchCurrency() {
      const resource = $("#batch-currency").value;
      const amountField = $("#batch-currency-amount");
      const amount = Number(amountField.value);
      if (!currencies.includes(resource) || !Number.isInteger(amount) || amount < 1 || amount > 1000000000) {
        $("#batch-currency-error").textContent = "Choose a currency and enter an amount from 1 to 1,000,000,000.";
        return;
      }
      batchItems.push({kind: "currency", resource, amount});
      itemGeneration++;
      $("#batch-currency-picker").hidden = true;
      renderBatchItems();
    }

    function drawBatchItemSelection() {
      const selected = $("#batch-selected-item"), searchArea = $("#batch-item-search-area");
      const options = $("#batch-item-options"), confirm = $("#confirm-batch-item");
      selected.innerHTML = pendingItem ? `<div class="selection"><div><strong>${esc(pendingItem.name)}</strong><small class="mono">${esc(pendingItem.template_id)}</small> · <small>${esc(pendingItem.category)} · ${esc(pendingItem.rarity_name)} · ${pendingItem.equipment ? "Equipment" : "Stack limit " + pendingItem.max_stack_size}</small></div><button id="change-batch-item" type="button" class="secondary">Change</button></div>` : "";
      searchArea.hidden = !!pendingItem;
      confirm.disabled = !pendingItem || !pendingItem.bookable;
      options.innerHTML = "";
      if (!pendingItem) return;
      $("#change-batch-item").addEventListener("click", () => {
        itemGeneration++; pendingItem = null; drawBatchItemSelection();
      });
      if (!pendingItem.bookable) {
        options.innerHTML = '<p class="error-text">This template cannot be booked as an inventory item. Choose another template or use a currency booking.</p>';
        return;
      }
      if (!pendingItem.equipment) {
        options.innerHTML = `<div class="form-grid"><label>Stack size<input name="batch_amount" type="number" min="1" max="${pendingItem.max_stack_size}" step="1" value="1" required><small>One stack, up to ${pendingItem.max_stack_size} units.</small></label></div>`;
        return;
      }
      const level = Math.min(pendingItem.max_level, Math.max(pendingItem.min_level, character?.level || pendingItem.min_level));
      options.innerHTML = `<p class="help">One equipment item per batch entry. Unique, set and mythic templates keep their authored rarity.</p><div class="form-grid">
        <label>Item level<input name="batch_item_level" type="number" min="${pendingItem.min_level}" max="${pendingItem.max_level}" step="1" value="${level}" required></label>
        <label>Rarity<select name="batch_rarity">${pendingItem.rarities.map(r => `<option value="${r.value}" ${r.value === pendingItem.rarity ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></label>
        <label>Open sockets<input name="batch_socket_count" type="number" min="0" step="1" value="0" required><small id="batch-socket-limit"></small></label>
        <label>Enchantment tier<select name="batch_enchantment_tier">${pendingItem.enchantment_tiers.map(t => `<option value="${t}" ${t === 1 ? "selected" : ""}>${t === 0 ? "0 · Normal" : t}</option>`).join("")}</select></label>
        <label>Enchantment roll quality<select name="batch-quality-mode"><option value="random">Random rolls</option><option value="fixed">Fixed percentage</option></select><small>Compatible enchantments are generated from the item's template, class, level and rarity.</small></label>
        <label id="batch-quality-field" hidden>Roll percentage<input name="batch-roll-quality" type="number" min="0" max="100" step="1" value="100" disabled><small>Applies to each generated enchantment.</small></label>
      </div>`;
      const syncSockets = () => {
        const max = pendingItem.socket_limits[Number(form.elements.namedItem("batch_item_level").value)] ?? 0;
        form.elements.namedItem("batch_socket_count").max = max;
        $("#batch-socket-limit").textContent = `Maximum ${max} at this item level.`;
      };
      form.elements.namedItem("batch_item_level").addEventListener("input", syncSockets);
      syncSockets();
      form.elements.namedItem("batch-quality-mode").addEventListener("change", () => {
        const fixed = form.elements.namedItem("batch-quality-mode").value === "fixed";
        $("#batch-quality-field").hidden = !fixed;
        form.elements.namedItem("batch-roll-quality").disabled = !fixed;
        form.elements.namedItem("batch-roll-quality").required = fixed;
      });
    }

    async function findBatchItem() {
      const version = ++itemGeneration, button = $("#find-batch-item"); button.disabled = true;
      $("#batch-item-error").textContent = "";
      try {
        const data = await api(`/api/items?limit=30&q=${encodeURIComponent($("#batch-item-search").value)}`);
        if (!alive() || version !== itemGeneration) return;
        $("#batch-item-results").innerHTML = table(["Name", "Template", "Category", "Rarity", ""], data.rows.map(i => [
          esc(i.name), `<code class="resource">${esc(i.template_id)}</code>`, esc(i.category), esc(i.rarity),
          `<button type="button" class="secondary" data-batch-template="${esc(i.template_id)}">Select</button>`
        ]));
        for (const select of $("#batch-item-results").querySelectorAll("[data-batch-template]")) select.addEventListener("click", async () => {
          const selectionVersion = ++itemGeneration; select.disabled = true;
          try {
            const selected = await api(`/api/items/${encodeURIComponent(select.dataset.batchTemplate)}`);
            if (!alive() || selectionVersion !== itemGeneration) return;
            pendingItem = selected; $("#batch-item-error").textContent = ""; drawBatchItemSelection();
          } catch (error) { if (alive()) $("#batch-item-error").textContent = error.message; }
          finally { select.disabled = false; }
        });
        if (data.total > data.rows.length) $("#batch-item-results").insertAdjacentHTML("beforeend", '<p class="help">Refine the search to find more items.</p>');
      } catch (error) { if (alive()) $("#batch-item-error").textContent = error.message; }
      finally { button.disabled = false; }
    }

    function confirmBatchItem() {
      if (!pendingItem || !pendingItem.bookable) return;
      const amountField = form.elements.namedItem("batch_amount");
      const amount = pendingItem.equipment ? 1 : Number(amountField?.value);
      if (!Number.isInteger(amount) || amount < 1 || amount > (pendingItem.equipment ? 1 : pendingItem.max_stack_size)) {
        $("#batch-item-error").textContent = `Enter a valid stack size (maximum ${pendingItem.equipment ? 1 : pendingItem.max_stack_size}).`;
        return;
      }
      let options = {};
      if (pendingItem.equipment) {
        const itemLevel = Number(form.elements.namedItem("batch_item_level").value);
        const rarity = Number(form.elements.namedItem("batch_rarity").value);
        const socketCount = Number(form.elements.namedItem("batch_socket_count").value);
        const enchantmentTier = Number(form.elements.namedItem("batch_enchantment_tier").value);
        const qualityMode = form.elements.namedItem("batch-quality-mode").value;
        const qualityField = form.elements.namedItem("batch-roll-quality");
        const rollQuality = qualityMode === "fixed" ? Number(qualityField.value) : null;
        if (!Number.isInteger(itemLevel) || itemLevel < pendingItem.min_level || itemLevel > pendingItem.max_level ||
            !pendingItem.rarities.some(option => option.value === rarity) ||
            !Number.isInteger(socketCount) || socketCount < 0 || socketCount > (pendingItem.socket_limits[itemLevel] ?? 0) ||
            !Number.isInteger(enchantmentTier) || !pendingItem.enchantment_tiers.includes(enchantmentTier) ||
            (rollQuality !== null && (!qualityField.value.trim() || !Number.isInteger(rollQuality) || rollQuality < 0 || rollQuality > 100))) {
          $("#batch-item-error").textContent = "Check the item level, sockets, enchantment tier, and roll percentage.";
          return;
        }
        options = {item_level: itemLevel, rarity, socket_count: socketCount, enchantment_tier: enchantmentTier, roll_quality: rollQuality};
      }
      batchItems.push({kind: "item", resource: pendingItem.template_id, amount, options, template_name: pendingItem.name});
      pendingItem = null; itemGeneration++;
      $("#batch-item-picker").hidden = true;
      renderBatchItems();
    }

    $("#add-item").addEventListener("click", () => addItemEntry("item"));
    $("#add-currency").addEventListener("click", () => addItemEntry("currency"));
    $("#find-batch-item").addEventListener("click", findBatchItem);
    $("#batch-item-search").addEventListener("keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); findBatchItem(); }
    });
    $("#cancel-batch-item").addEventListener("click", () => {
      itemGeneration++; pendingItem = null; $("#batch-item-picker").hidden = true;
    });
    $("#confirm-batch-item").addEventListener("click", confirmBatchItem);
    $("#cancel-batch-currency").addEventListener("click", () => {
      itemGeneration++; $("#batch-currency-picker").hidden = true;
    });
    $("#confirm-batch-currency").addEventListener("click", confirmBatchCurrency);
    $("#batch-currency-amount").addEventListener("keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); confirmBatchCurrency(); }
    });

    form.addEventListener("submit", async event => {
      event.preventDefault();
      const button = form.querySelector("button[type=submit]");
      button.disabled = true;
      try {
        if (!account) throw new Error("Select an account first.");
        character = account.characters.find(c => c.character_id === Number(field("character_id").value));
        if (!character) throw new Error("Select a character.");
        if (!batchItems.length) throw new Error("Add at least one item or currency.");
        prepared = {
          account_id: account.account.account_id,
          character_id: character.character_id,
          items: batchItems.map(i => ({kind: i.kind, resource: i.resource, amount: i.amount, options: i.options})),
          request_id: requestId()
        };
        const rows = [
          ["Account", esc(account.account.username)],
          ["Character", `${esc(character.name)} · ${character.character_id}`],
          ["Items", batchItems.length]
        ];
        const itemList = batchItems.map(i => {
          const name = i.kind === "currency" ? human(i.resource) : (i.template_name || i.resource);
          return `${esc(name)} × ${i.amount}`;
        }).join("<br>");
        rows.push(["Grants", itemList]);
        $("#batch-review").innerHTML = section("Review batch booking", facts(rows)) +
          '<p class="muted">All grants are delivered together when the character is ready online, or queued for their next game entry.</p><p id="review-error" class="error-text" role="alert"></p><div class="form-actions"><button id="edit-batch" class="secondary">Back to editing</button><button id="queue-batch">Create batch booking</button></div>';
        form.hidden = true; $("#batch-review").hidden = false;
        $("#edit-batch").addEventListener("click", () => { form.hidden = false; $("#batch-review").hidden = true; });
        $("#queue-batch").addEventListener("click", async event => {
          const button = event.target; button.disabled = true; $("#edit-batch").disabled = true;
          try {
            const reason = await confirmAction("Create this batch booking?", `Deliver ${prepared.items.length} ${prepared.items.length === 1 ? "grant" : "grants"} together to ${character.name}.`, "Create batch booking");
            if (reason === null) return;
            const result = await api("/api/bookings/batch", {...prepared, reason});
            notice(result.status === "delivered" ? "Batch booking delivered to the online character." : result.error ? "Batch booking saved. Review the delivery issue below." : "Batch booking queued for the character's next game entry.");
            navigate(`bookings/${result.id}`);
          } catch (error) { if (alive()) $("#review-error").textContent = error.message; }
          finally { button.disabled = false; if (alive()) $("#edit-batch").disabled = false; }
        });
        $("#batch-review").scrollIntoView({block: "start"});
      } catch (error) { formError(error); }
      finally { button.disabled = false; }
    });

    $("#find-account").addEventListener("click", findAccount);
    field("character_id").addEventListener("change", () => {
      character = account?.characters.find(c => c.character_id === Number(field("character_id").value));
      drawCharacterNote();
    });
    drawAccount(); renderBatchItems();
  };
  return result;
}
