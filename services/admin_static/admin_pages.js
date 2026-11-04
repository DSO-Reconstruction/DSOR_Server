import { $, isLegacyInterface, esc, human, date, api, href, link, navigate, status, table, facts, stats, section, collectionPanel, collectionWorkspace,
  tabs, toolbar, bindSearch, pager, formError, confirmAction, notice, tree,
  characterTable, characterHeaders, characterRows, bookingTable, bookingHeaders, bookingRows, bookingGrants, recordCards, collectionRecords } from "./admin_ui.js";

const names = {status: "Overview", accounts: "Accounts", characters: "Characters", bookings: "Bookings", items: "Item catalog", sessions: "Sessions", audit: "Audit log"};
const query = params => new URLSearchParams({limit: 50, ...params});
const accountTabs = id => [["Characters", `accounts/${id}`], ["Membership", `accounts/${id}/membership`], ["Currencies", `accounts/${id}/currencies`], ["Settings", `accounts/${id}/settings`], ["Sessions", `accounts/${id}/sessions`], ["Bookings", `accounts/${id}/bookings`]];

const membershipName = m => !m ? "No saved state" : m.active ? ({1: "Premium", 2: "Deluxe"}[m.type] || "Active") : "Inactive";
const membershipExpiry = m => m?.expires_at ? date(m.expires_at) : "—";

function accountMembershipPage(id, account, characters) {
  const allOffline = characters.every(character => !character.online);
  const allSaved = characters.length > 0 && characters.length === account.character_ids.length && characters.every(character => character.has_state);
  const enabled = allOffline && allSaved;
  const rows = characters.map(character => [
    link(character.name, `characters/${character.character_id}`),
    membershipName(character.membership), membershipExpiry(character.membership),
    character.membership?.premium_expires_at ? date(character.membership.premium_expires_at) : "—",
    character.online ? status("Online", "bad") : status("Offline", "good"),
  ]);
  const form = section("Edit account membership", `<form id="account-membership-form" class="form-page">
    <fieldset class="form-section" ${enabled ? "" : "disabled"}><legend>Premium and Deluxe</legend><div class="form-grid">
      <label>Plan<select name="tier"><option value="1">Premium</option><option value="2">Deluxe</option></select></label>
      <label>Duration in days<input name="days" type="number" min="1" max="3650" step="1" value="30" required></label>
    </div><p class="help">Grants or extends the selected plan for every character on this account. Removing Deluxe resumes remaining Premium time.</p></fieldset>
    <p id="membership-error" class="error-text" role="alert"></p>
    <div class="form-actions"><button type="submit" name="action" value="grant" ${enabled ? "" : "disabled"}>Grant / extend</button><button type="submit" class="danger" name="action" value="remove" ${enabled ? "" : "disabled"}>Remove selected plan</button></div>
    ${!allOffline ? '<p class="help">Sign out all characters on this account before editing membership.</p>' : ""}
    ${!allSaved ? '<p class="help">Every character must have a saved gameplay state before account membership can be edited.</p>' : ""}</form>`);
  return section("Character memberships", table(["Character", "Plan", "Expires", "Premium after Deluxe", "Presence"], rows)) + form;
}

function bindAccountMembership(id, account) {
  const form = $("#account-membership-form");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const button = event.submitter, action = button?.value;
    if (!action) return;
    const values = new FormData(form), tier = Number(values.get("tier")), days = Number(values.get("days"));
    const plan = tier === 1 ? "Premium" : "Deluxe";
    button.disabled = true;
    try {
      if (action === "grant" && (!Number.isInteger(days) || days < 1 || days > 3650)) throw new Error("Duration must be a whole number from 1 to 3650 days.");
      const summary = action === "grant" ? `grant ${plan} for ${days} days to all characters on ${account.username}` : `remove ${plan} from all characters on ${account.username}`;
      const reason = await confirmAction("Change account membership?", `${summary}. All characters must be offline.`, action === "grant" ? "Grant membership" : "Remove membership");
      if (reason === null) return;
      const result = await api(`/api/accounts/${id}/membership`, {tier, action, days, reason});
      notice(`${plan} updated for ${result.characters_updated} character(s).`);
      window.dispatchEvent(new Event("reload-page"));
    } catch (error) { const target = $("#membership-error"); if (target) target.textContent = error.message; else notice(error.message, true); }
    finally { button.disabled = false; }
  });
}

const fixedCurrencyFields = [
  ["andermant", "Andermant"], ["gold", "Gold", true], ["badge_of_honor", "Badge of Honor"],
  ["legacy_progression", "Legacy progression"], ["draken", "Draken"], ["glyph_of_power", "Glyphs of Power", true],
];

function accountCurrenciesPage(id, characters) {
  const allOffline = characters.every(character => !character.online);
  return characters.map(character => {
    const enabled = character.has_state && allOffline;
    const fields = fixedCurrencyFields.map(([key, label, wide]) => `<label>${label}<input name="${key}" type="${wide ? "text" : "number"}" ${wide ? 'inputmode="numeric" maxlength="20" pattern="[0-9]+"' : 'min="0" max="4294967295" step="1"'} value="${esc(character.currencies?.[key] ?? 0)}" required></label>`).join("");
    const additional = Object.entries(character.additional_currencies || {}).map(([key, value]) => `<label>${esc(human(key))}<input name="game:${esc(key)}" type="text" inputmode="numeric" maxlength="20" pattern="[0-9]+" value="${esc(value)}" required></label>`).join("");
    return section(esc(character.name), `<form class="account-currency-form" data-character-id="${character.character_id}">
      <fieldset class="form-section" ${enabled ? "" : "disabled"}><legend>Currencies</legend><div class="form-grid">${fields}</div><p class="help">Andermant is shared by all characters on this account.</p></fieldset>
      <fieldset class="form-section" ${enabled ? "" : "disabled"}><legend>Additional currencies</legend><div class="form-grid">${additional}</div></fieldset>
      ${!character.has_state ? '<p class="help">This character has no saved gameplay state yet.</p>' : ""}${!allOffline ? '<p class="help">Sign out all characters on this account before editing currencies.</p>' : ""}
      <p class="error-text" role="alert"></p><div class="form-actions"><button type="submit" ${enabled ? "" : "disabled"}>Save currencies</button></div></form>`);
  }).join("") || '<p class="empty">This account has no characters.</p>';
}

function bindAccountCurrencies(id) {
  document.querySelectorAll(".account-currency-form").forEach(form => form.addEventListener("submit", async event => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]'), values = new FormData(form), changes = {additional_currencies: {}};
    for (const [key, value] of values.entries()) {
      if (key.startsWith("game:")) changes.additional_currencies[key.slice(5)] = String(value);
      else changes[key] = ["gold", "glyph_of_power"].includes(key) ? String(value) : Number(value);
    }
    button.disabled = true;
    try {
      for (const [key, value] of Object.entries(changes)) {
        if (key === "additional_currencies") continue;
        if (["gold", "glyph_of_power"].includes(key)) {
          if (!/^\d{1,20}$/.test(value) || BigInt(value) > 18446744073709551615n) throw new Error(`${human(key)} must be a whole number from 0 to 18,446,744,073,709,551,615.`);
        } else if (!Number.isSafeInteger(value) || value < 0 || value > 4294967295) throw new Error(`${human(key)} must be a whole number from 0 to 4,294,967,295.`);
      }
      for (const [key, value] of Object.entries(changes.additional_currencies)) {
        if (!/^\d{1,20}$/.test(value) || BigInt(value) > 18446744073709551615n) throw new Error(`${human(key)} must be a valid non-negative whole number.`);
      }
      const characterId = Number(form.dataset.characterId);
      const reason = await confirmAction("Save character currencies?", `Update the saved balances for character ${characterId}.`, "Save currencies");
      if (reason === null) return;
      await api(`/api/accounts/${id}/currencies`, {character_id: characterId, changes, reason});
      notice("Character currencies saved."); window.dispatchEvent(new Event("reload-page"));
    } catch (error) { const target = form.querySelector('[role="alert"]'); if (target) target.textContent = error.message; else notice(error.message, true); }
    finally { button.disabled = false; }
  }));
}
export async function listPage(route) {
  const name = route.parts[0];
  const data = await api(`/api/${name}?${query(route.params)}`);
  const result = {title: names[name], breadcrumbs: [["Administration", "status"], [names[name]]], mount: () => bindSearch(route)};
  if (name === "status") {
    result.description = "Realm activity, service health, and operational queues.";
    if (isLegacyInterface()) {
      result.actions = link("Server control", "server-control", {}, "button");
      result.description = "Service health and current activity.";
      result.html = stats([["Accounts", data.accounts], ["Characters", data.characters], ["Online", data.online_characters], ["Pending bookings", data.bookings.pending || 0], ["Uptime", Math.floor(data.uptime_seconds / 3600) + "h " + Math.floor(data.uptime_seconds % 3600 / 60) + "m"]]) +
        section("Services", table(["Service", "Port", "State", "Connections"], data.services.map(s => [esc(s.name), s.port, status(s.running ? "Running" : "Stopped", s.running ? "good" : "bad"), s.connections ?? "—"]))) +
        '<div class="columns">' + section("Booking delivery", facts([["Pending", esc(data.bookings.pending || 0)], ["Delivered", esc(data.bookings.delivered || 0)], ["Cancelled", esc(data.bookings.cancelled || 0)]]), link("View bookings", "bookings")) +
        section("Runtime", tree(data.watchdog || {})) + '</div>';
      if (data.warnings.length) result.html += section("Persistence warnings", table(["Warning"], data.warnings.map(w => [esc(w)])));
      return result;
    }
    const audit = await api("/api/audit?limit=5").catch(() => null);
    const recentActivity = audit?.rows?.length
      ? `<ol class="realm-activity">${audit.rows.map(entry => `<li>
          ${link(human(entry.action), `audit/${entry.id}`)}
          <small>${esc(entry.actor)} · ${esc(date(entry.created_at))}</small>
        </li>`).join("")}</ol>`
      : `<p class="muted">${audit ? "No administrator activity yet." : "Recent activity is unavailable."}</p>`;
    const gameOnline = data.services.some(s => !["Admin HTTP", "Admin CMS"].includes(s.name) && s.running);
    const serviceHealth = data.services.length ? Math.round(data.services.filter(s => s.running).length / data.services.length * 100) : 0;
    const serviceTiles = data.services.map(service => `<article class="service-tile ${service.running ? "service-up" : "service-down"}"><div class="service-tile-head"><span class="service-mark" aria-hidden="true">${service.running ? "✦" : "—"}</span><strong>${esc(service.name)}</strong>${status(service.running ? "Running" : "Stopped", service.running ? "good" : "bad")}</div><code>${service.port == null ? "HTTP service" : `Port ${esc(service.port)}`}</code><small>${service.connections == null ? "Administrative endpoint" : `${esc(service.connections)} active connection(s)`}</small></article>`).join("");
    const quickActions = `<div class="quick-actions">${link("Create account", "accounts/new")}${link("Prepare a player grant", "bookings/new")}${link("Create bonus code", "bonus-codes/new")}${link("Manage live events", "events")}</div>`;
    const uptime = Math.floor(data.uptime_seconds / 3600) + "h " + Math.floor(data.uptime_seconds % 3600 / 60) + "m";
    result.html = `<div class="realm-board">
      <section class="realm-feature">
        <div class="realm-feature-copy">
          <p class="realm-eyebrow"><span aria-hidden="true"></span> LIVE STATUS</p>
          <h2>Realm Overview</h2>
          <p class="realm-feature-summary">A live view of the world, its services, and the people in it.</p>
          ${link("Open server controls", "server-control", {}, "button")}
        </div>
        <span class="realm-feature-mark" aria-hidden="true">DSO</span>
      </section>
      <section class="realm-vitals"><h2>Server status</h2>
        ${status(gameOnline ? "Game services online" : "Game services offline", gameOnline ? "good" : "bad")}
        <div class="realm-health">
          <svg viewBox="0 0 100 100" aria-hidden="true">
            <circle class="realm-health-track" cx="50" cy="50" r="44"/>
            <circle class="realm-health-value" cx="50" cy="50" r="44" pathLength="100" stroke-dasharray="${serviceHealth} 100" transform="rotate(-90 50 50)"/>
          </svg>
          <div><strong>${serviceHealth}%</strong><small>services running</small></div>
        </div>
        <p>Uptime <strong>${esc(uptime)}</strong></p>
      </section>
      <aside class="realm-operations">${section("Quick actions", quickActions)}${section("Recent activity", recentActivity, link("Audit log", "audit"))}${section("Runtime telemetry", tree(data.watchdog || {}))}</aside>
      <div class="realm-metrics">${stats([["Players online", data.online_characters], ["Accounts", data.accounts], ["Characters", data.characters], ["Pending grants", data.bookings.pending || 0]])}</div>
      <div class="realm-services">${section("Realm services", `<div class="service-grid">${serviceTiles}</div>`)}
        ${section("Grant ledger", facts([["Awaiting delivery", esc(data.bookings.pending || 0)], ["Delivered", esc(data.bookings.delivered || 0)], ["Cancelled", esc(data.bookings.cancelled || 0)]]), link("Open ledger", "bookings"))}
        ${data.warnings.length ? section("Persistence warnings", table(["Warning"], data.warnings.map(w => [esc(w)]))) : ""}
      </div>
    </div>`;
    return result;
  }
  let rows = "", headers = [];
  let extra = "";
  let searchHint = "Search " + names[name].toLowerCase();
  if (name === "accounts") {
    result.description = "Open an account to manage access and inspect its characters.";
    result.actions = link("Create account", "accounts/new", {}, "button");
    headers = ["Account", "ID", "Access", "State", "Characters", "Last login", "Created"];
    rows = data.rows.map(a => [
      link(a.username, `accounts/${a.account_id}`), a.account_id, a.admin ? "Administrator" : "Player",
      status(a.disabled ? "Disabled" : "Active", a.disabled ? "bad" : "good"), a.character_ids.length,
      esc(date(a.last_login_at)), esc(date(a.created_at))
    ]);
  } else if (name === "characters") {
    result.description = "Find a character by name, ID, or account.";
    headers = characterHeaders(true); rows = characterRows(data.rows, true);
  } else if (name === "bookings") {
    result.description = "Queued grants and their delivery history.";
    result.actions = link("New booking", "bookings/new", {}, "button");
    headers = bookingHeaders(); rows = bookingRows(data.rows);
  } else if (name === "items") {
    result.description = "Game item templates. Open a template to inspect its properties and book it.";
    searchHint = "Search by in-game name, category, or template ID";
    const sort = route.params.sort || "template";
    extra = `<select id="item-category" aria-label="Category"><option value="">All categories</option>${(data.categories || [])
        .map(value => `<option value="${esc(value)}" ${route.params.category === value ? "selected" : ""}>${esc(value)}</option>`).join("")}</select>` +
      '<span class="spacer"></span><label for="item-sort">Sort by</label><select id="item-sort" name="sort">' +
      [["template", "Template ID"], ["name", "Name (A-Z)"], ["category", "Category (A-Z)"], ["category_desc", "Category (Z-A)"]]
        .map(([value, label]) => `<option value="${value}" ${sort === value ? "selected" : ""}>${label}</option>`).join("") + '</select>';
    result.mount = () => {
      bindSearch(route);
      $("#item-category").addEventListener("change", event => navigate(route.path, {...route.params, category: event.target.value, offset: ""}));
      $("#item-sort").addEventListener("change", event => navigate(route.path, {...route.params, sort: event.target.value, offset: ""}));
    };
    headers = ["Name", "Template", "Category", "Rarity", "Required level", "Stack limit"];
    rows = data.rows.map(i => [
      esc(i.name),
      link(i.template_id, `items/${encodeURIComponent(i.template_id)}`, route.params, "resource mono"),
      esc(i.category), esc(i.rarity), esc(i.required_level), esc(i.max_stack_size)
    ]);
  } else if (name === "sessions") {
    result.description = "Issued game login sessions. Open the account to revoke its sessions.";
    headers = ["Account", "Address", "Issued", "Expires"];
    rows = data.rows.map(s => [link(s.username, `accounts/${s.account_id}/sessions`), esc(s.client_ip), esc(date(s.issued_at)), esc(date(s.expires_at))]);
  } else if (name === "audit") {
    result.description = "Administrator actions, recorded with a reason and target.";
    headers = ["Entry", "Time", "Administrator", "Action", "Target", "Reason"];
    rows = data.rows.map(a => [
      link("#" + a.id, `audit/${a.id}`), esc(date(a.created_at)), esc(a.actor), esc(a.action), esc(a.target), esc(a.details.reason || "—")
    ]);
  }
  const entryCount = Number(data.total ?? data.rows.length);
  const classicList = toolbar(route, searchHint, extra) + table(headers, rows) + pager(data, route);
  const directoryViews = ["accounts", "characters"].includes(name);
  const modernRecords = directoryViews ? recordCards(headers, rows) : table(headers, rows);
  const modernList = `<div class="collection-workspace">
    <section class="collection-panel ${directoryViews ? "collection-directory" : "collection-ledger"}" aria-label="${esc(names[name])} records">
      <header class="collection-toolbar">
        ${toolbar(route, searchHint, extra)}
        <p class="collection-total"><strong>${esc(entryCount.toLocaleString())}</strong> ${entryCount === 1 ? "record" : "records"}</p>
      </header>
      ${modernRecords}
      <footer class="collection-panel-foot">${pager(data, route)}</footer>
    </section>
  </div>`;
  result.html = isLegacyInterface() ? classicList : modernList;
  return result;
}

function sessionTable(rows) {
  return table(["Account", "Address", "Issued", "Expires"], rows.map(s => [
    link(s.username, `accounts/${s.account_id}/sessions`), esc(s.client_ip), esc(date(s.issued_at)), esc(date(s.expires_at))
  ]), "No active game login sessions.");
}

export async function accountPage(route) {
  const id = route.parts[1], tab = route.parts[2] || "characters";
  if (id === "new") return newAccountPage();
  const data = await api(`/api/accounts/${id}`), a = data.account;
  const result = {
    title: a.username, description: `Account ${a.account_id} · ${a.admin ? "Administrator" : "Player"} · ${a.disabled ? "Disabled" : "Active"}`,
    breadcrumbs: [["Accounts", "accounts"], [a.username, `accounts/${id}`], ...(tab === "characters" ? [] : [[human(tab)]])],
    actions: link("New booking", "bookings/new", {account_id: id}, "button"),
    html: tabs(accountTabs(id), route.path)
  };
  if (tab === "characters") {
    const metrics = stats([["Characters", data.characters.length], ["Online", data.characters.filter(c => c.online).length], ["Created", date(a.created_at)], ["Last login", date(a.last_login_at)]]);
    const launch = section("Launch game", `<div class="form-grid"><label>Client language<select id="launch-language"><option value="en">English</option><option value="tr">Turkish</option></select></label><div class="form-actions"><button id="launch-account" type="button">Start game as ${esc(a.username)}</button></div></div><p class="help">Starts the game client on this computer using a fresh session for this account. The account must be enabled, and the client must be installed here. Existing sessions for this account are revoked.</p><p id="launch-error" class="error-text" role="alert"></p>`);
    const copyAccounts = await api("/api/accounts?limit=100");
    const copy = section("Copy characters to an account", `<form id="copy-characters-form" class="form-page">
      <fieldset class="form-section"><legend>Select characters</legend><div class="form-stack">${data.characters.map(c => `<label class="check"><input type="checkbox" name="copy-character" value="${c.character_id}"><span>${esc(c.name)} · Level ${c.level} · ID ${c.character_id}</span></label><label>Copied character name<input name="copy-name-${c.character_id}" value="${esc(c.name.slice(0, 26) + "_copy")}" maxlength="32" required></label>`).join("") || '<p class="empty">This account has no characters with saved profiles.</p>'}</div></fieldset>
      <fieldset class="form-section"><legend>Destination account</legend><div class="form-grid"><label>Destination<select id="copy-destination"><option value="new">Create a new account</option>${copyAccounts.rows.map(row => `<option value="${row.account_id}">${esc(row.username)} · ${row.account_id}</option>`).join("")}</select></label>
        <label id="copy-username-label">New username<input id="copy-username" minlength="3" maxlength="24" autocomplete="off"></label><label id="copy-password-label">Initial password<input id="copy-password" type="password" minlength="6" maxlength="256" autocomplete="new-password"></label>
      </div><p class="help">Copies saved profile and gameplay data. Selected characters must be offline. Shared account currency uses the destination account's wallet.</p></fieldset>
      <p id="copy-error" class="error-text" role="alert"></p><div class="form-actions"><button type="submit">Copy selected characters</button></div></form>`);
    const missing = data.missing_character_ids.length
      ? `<p class="muted">Missing character profiles: ${esc(data.missing_character_ids.join(", "))}</p>` : "";
    if (isLegacyInterface()) {
      result.html += metrics + section("Characters", characterTable(data.characters)) + launch + copy + missing;
    } else {
      const roster = collectionRecords(characterHeaders(), characterRows(data.characters), "No characters are tied to this account.");
      result.html += `<div class="account-workspace">
        <div class="account-roster">${metrics}${section("Character roster", roster + missing)}</div>
        <aside aria-label="Account operations">${launch}${copy}</aside>
      </div>`;
    }
    result.mount = () => {
      $("#launch-account").addEventListener("click", async event => {
      const button = event.currentTarget; button.disabled = true;
      try {
        const reason = await confirmAction("Start game as this account?", `Open the local game client as ${a.username}.`, "Start game");
        if (reason === null) return;
        await api(`/api/accounts/${id}/launch`, {reason, language: $("#launch-language").value});
        notice(`Starting the game as ${a.username}.`);
      } catch (error) { const target = $("#launch-error"); if (target) target.textContent = error.message; }
      finally { button.disabled = false; }
      });
      const destination = $("#copy-destination"), username = $("#copy-username"), password = $("#copy-password");
      const syncDestination = () => {
        const creating = destination.value === "new";
        username.required = password.required = creating;
        username.disabled = password.disabled = !creating;
        $("#copy-username-label").hidden = $("#copy-password-label").hidden = !creating;
      };
      destination.addEventListener("change", syncDestination); syncDestination();
      $("#copy-characters-form").addEventListener("submit", async event => {
        event.preventDefault(); const form = event.target, button = form.querySelector("button[type=submit]"); button.disabled = true;
        try {
          const selected = [...form.querySelectorAll('input[name="copy-character"]:checked')];
          if (!selected.length) throw new Error("Select at least one character to copy.");
          const characters = selected.map(input => {
            const characterId = Number(input.value);
            return {character_id: characterId, name: form.elements[`copy-name-${characterId}`].value};
          });
          const isNew = destination.value === "new";
          const targetName = isNew ? username.value : destination.selectedOptions[0].textContent;
          const reason = await confirmAction("Copy selected characters?", `Copy ${characters.length} character(s) from ${a.username} to ${targetName}.`, "Copy characters");
          if (reason === null) return;
          const payload = {characters, reason};
          if (isNew) { payload.username = username.value; payload.password = password.value; }
          else payload.destination_account_id = Number(destination.value);
          const copied = await api(`/api/accounts/${id}/copy-characters`, payload);
          notice(`Copied ${copied.copied} character(s) to ${copied.account.username}.`);
          navigate(`accounts/${copied.account.account_id}`);
        } catch (error) { const target = $("#copy-error"); if (target) target.textContent = error.message; }
        finally { button.disabled = false; }
      });
    };
  } else if (tab === "membership") {
    result.html += accountMembershipPage(id, a, data.characters);
    result.mount = () => bindAccountMembership(id, a);
  } else if (tab === "currencies") {
    result.html += accountCurrenciesPage(id, data.characters);
    result.mount = () => bindAccountCurrencies(id);
  } else if (tab === "settings") {
    const profileForm = `<form id="account-form" class="form-page">
      <fieldset class="form-section"><legend>Identity</legend><div class="form-grid">
        <label>Username<input name="username" value="${esc(a.username)}" required minlength="3" maxlength="24" autocomplete="off"></label>
        <label>New password<input name="password" type="password" minlength="6" maxlength="256" autocomplete="new-password"><small>Leave empty to keep the current password.</small></label>
      </div></fieldset>
      <fieldset class="form-section"><legend>Access</legend><div class="form-stack">
        <label class="check"><input name="admin" type="checkbox" ${a.admin ? "checked" : ""}>CMS administrator</label>
        <label class="check"><input name="disabled" type="checkbox" ${a.disabled ? "checked" : ""}>Disable account login</label>
        <p class="help">Changes revoke issued login sessions. An already connected game client remains connected until it leaves.</p>
      </div></fieldset>
      <p id="form-error" class="error-text" role="alert"></p>
      <div class="form-actions"><button type="submit">Save changes</button></div></form>`;
    const deleteForm = `<form id="delete-account-form" class="form-page"><h2>Delete account</h2><p class="help">Permanently removes this account, its character records and saved character data. Characters must be offline. This cannot be undone.</p><p id="delete-error" class="error-text" role="alert"></p><div class="form-actions"><button type="submit" class="danger">Delete account</button></div></form>`;
    result.html += isLegacyInterface() ? profileForm + deleteForm
      : `<div class="detail-workspace"><div>${profileForm}</div><aside aria-label="Account deletion">${deleteForm}</aside></div>`;
    result.mount = () => {
      $("#account-form").addEventListener("submit", async event => {
      event.preventDefault(); const form = event.target, button = form.querySelector("button[type=submit]");
      const values = new FormData(form), changes = {};
      if (values.get("username") !== a.username) changes.username = values.get("username");
      if (values.get("password")) changes.password = values.get("password");
      if (values.has("admin") !== a.admin) changes.admin = values.has("admin");
      if (values.has("disabled") !== a.disabled) changes.disabled = values.has("disabled");
      button.disabled = true;
      try {
        if (!Object.keys(changes).length) throw new Error("No changes to save.");
        const reason = await confirmAction("Save account changes?", `Update ${a.username} and revoke its issued login sessions.`, "Save changes");
        if (reason === null) return;
        await api(`/api/accounts/${id}`, {changes, reason});
        notice("Account updated. Issued login sessions have been revoked."); navigate(`accounts/${id}`);
      } catch (error) { formError(error); } finally { button.disabled = false; }
      });
      $("#delete-account-form").addEventListener("submit", async event => {
        event.preventDefault(); const button = event.target.querySelector("button"); button.disabled = true;
        try {
          const reason = await confirmAction("Delete account permanently?", `${a.username} and all ${a.character_ids.length} owned character(s) and their saved data will be removed.`, "Delete account");
          if (reason === null) return;
          await api(`/api/accounts/${id}/delete`, {reason});
          notice(`Deleted ${a.username} and its characters.`); navigate("accounts");
        } catch (error) {
          const target = $("#delete-error"); if (target) target.textContent = error.message;
        } finally { button.disabled = false; }
      });
    };
  } else if (tab === "sessions") {
    const sessions = await api(`/api/sessions?${query({...route.params, account_id: id})}`);
    const revoke = `<form id="revoke-form" class="form-page"><h2>Revoke all sessions</h2><p class="muted">Signs this account out of the CMS and prevents reuse of issued game login tokens. Current game connections are not disconnected.</p><p id="form-error" class="error-text" role="alert"></p><div class="form-actions"><button type="submit" class="secondary">Revoke sessions</button></div></form>`;
    const list = sessionTable(sessions.rows), pagination = pager(sessions, route);
    result.html += isLegacyInterface()
      ? section("Game login sessions", list) + pagination + revoke
      : `<div class="detail-workspace">
          ${collectionPanel("Game login sessions", list, pagination, sessions.total, "Issued game login tokens for this account.")}
          <aside aria-label="Session controls">${revoke}</aside>
        </div>`;
    result.mount = () => $("#revoke-form").addEventListener("submit", async event => {
      event.preventDefault(); const button = event.target.querySelector("button"); button.disabled = true;
      try {
        const reason = await confirmAction("Revoke all sessions?", `Revoke all issued login sessions for ${a.username}.`, "Revoke sessions");
        if (reason === null) return;
        await api(`/api/accounts/${id}/revoke`, {reason});
        notice("Sessions revoked."); window.dispatchEvent(new Event("reload-page"));
      } catch (error) { formError(error); } finally { button.disabled = false; }
    });
  } else if (tab === "bookings") {
    const bookings = await api(`/api/bookings?${query({...route.params, account_id: id})}`);
    result.html += collectionWorkspace(toolbar(route, "Search this account's bookings"),
      collectionPanel("Booking history", collectionRecords(bookingHeaders(), bookingRows(bookings.rows)), pager(bookings, route), bookings.total,
        "Rewards and delivery state for this account."));
    result.mount = () => bindSearch(route);
  } else throw new Error("Account page not found.");
  return result;
}

function newAccountPage() {
  return {
    title: "Create account", description: "Add a game account with a username and initial password.",
    breadcrumbs: [["Accounts", "accounts"], ["Create account"]],
    html: `<form id="create-account-form" class="form-page"><fieldset class="form-section"><legend>Account credentials</legend><div class="form-grid">
      <label>Username<input name="username" required minlength="3" maxlength="24" autocomplete="off" pattern="[A-Za-z0-9][A-Za-z0-9_.-]{2,23}"></label>
      <label>Initial password<input name="password" type="password" required minlength="6" maxlength="256" autocomplete="new-password"></label>
    </div></fieldset><p id="form-error" class="error-text" role="alert"></p><div class="form-actions"><button type="submit">Create account</button></div></form>`,
    mount: () => $("#create-account-form").addEventListener("submit", async event => {
      event.preventDefault(); const form = event.target, button = form.querySelector("button"); button.disabled = true;
      try {
        const values = new FormData(form), username = String(values.get("username")), password = String(values.get("password"));
        const reason = await confirmAction("Create game account?", `Create ${username} with the entered initial password.`, "Create account");
        if (reason === null) return;
        const created = await api("/api/accounts", {username, password, reason});
        notice(`Account ${created.account.username} created.`); navigate(`accounts/${created.account.account_id}`);
      } catch (error) { formError(error); } finally { button.disabled = false; }
    })
  };
}

export async function itemPage(route) {
  const id = decodeURIComponent(route.parts.slice(1).join("/"));
  const item = await api(`/api/items/${encodeURIComponent(id)}`);
  const template = section("Template", facts([
      ["Name", esc(item.name)], ["Template ID", `<code class="resource mono">${esc(id)}</code>`],
      ["Internal name", esc(item.template_name || "—")],
      ["Category", esc(item.category)], ["Default rarity", esc(item.rarity_name)], ["Required level", item.required_level],
      ["Stack limit", item.max_stack_size], ["Destination", esc(item.destination)], ["Classes", esc(item.classes.join(", ") || "All classes")],
      ["Slots", esc(item.slot_types.join(", ") || "None")]
    ]));
  const booking = section("Booking controls", item.equipment ? facts([
      ["Item level", `${item.min_level}–${item.max_level}`], ["Rarities", esc(item.rarities.map(r => r.name).join(", "))],
      ["Sockets", "Limited by item level and template"], ["Enchantments", "Generated using the game's equipment rules"], ["Roll quality", "Random or a fixed percentage"]
    ]) : `<p>${item.bookable ? "Only stack size can be set for this item." : "This template cannot be booked as an inventory item. Use a currency booking where applicable."}</p>`);
  const sockets = (item.equipment ? section("Socket limits", table(["Level range", "Maximum sockets"], socketRanges(item).map(r => [r.range, r.limit]))) : "");
  const html = isLegacyInterface()
    ? `<div class="columns">${template}${booking}</div>${sockets}`
    : `<div class="detail-workspace">
        <div>${template}${sockets}</div>
        <aside aria-label="Booking options">${booking}</aside>
      </div>`;
  return {
    title: item.name, description: `${id} · ${item.category} · ${item.equipment ? "Equipment" : "Stack item"}`,
    breadcrumbs: [["Item catalog", "items", route.params], [item.name]],
    actions: item.bookable ? link("Book item", "bookings/new", {template: id, account_id: route.params.account_id, character_id: route.params.character_id}, "button") : "",
    html
  };
}

function socketRanges(item) {
  const rows = []; let start = item.min_level, previous = item.socket_limits[start];
  for (let level = start + 1; level <= item.max_level + 1; level++) {
    if (item.socket_limits[level] !== previous || level > item.max_level) {
      rows.push({range: start === level - 1 ? String(start) : `${start}–${level - 1}`, limit: previous});
      start = level; previous = item.socket_limits[level];
    }
  }
  return rows;
}

export async function bookingPage(route) {
  const b = await api(`/api/bookings/${route.parts[1]}`);
  const grant = section("Recipient and grant", facts([
      ["Account", link(b.account.username, `accounts/${b.account_id}`)], ["Character", link(b.character?.name || b.character_id, `characters/${b.character_id}`)],
      ["Resource", b.kind === "item" ? link(b.resource, `items/${encodeURIComponent(b.resource)}`, {}, "resource mono") : b.kind === "batch" ? bookingGrants(b) : esc(human(b.resource))],
      ["Quantity", b.amount], ["Reason", esc(b.reason)]
    ]));
  const delivery = section("Delivery", facts([
      ["Status", status(human(b.status), b.status === "delivered" ? "good" : b.error ? "bad" : "")],
      ["Created", esc(date(b.created_at))], ["Administrator", esc(b.actor)], ["Delivered", esc(date(b.delivered_at))], ["Delivery issue", esc(b.error || "None")]
    ]));
  const equipment = (Object.keys(b.options).length ? section("Equipment configuration", tree(b.options)) : "");
  let controls = "";
  const result = {
    title: `Booking #${b.id}`, description: `${human(b.status)} · ${b.kind === "batch" ? "Batch" : b.kind === "currency" ? "Currency" : "Item"} grant`,
    breadcrumbs: [["Bookings", "bookings"], [`#${b.id}`]],
    actions: link("New booking", "bookings/new", {account_id: b.account_id, character_id: b.character_id}, "button secondary"),
    html: ""
  };
  if (b.status === "pending") {
    controls = `<form id="cancel-form" class="form-page"><h2>Pending booking actions</h2><p class="muted">Retry delivery if the character is ready online. Otherwise, this booking remains queued for their next game entry. You can cancel it before it is applied.</p><p id="form-error" class="error-text" role="alert"></p><div class="form-actions"><button id="retry-booking" type="button">Retry live delivery</button><button class="secondary" type="submit">Cancel booking</button></div></form>`;
    result.mount = () => {
      $("#retry-booking").addEventListener("click", async event => {
        const button = event.target;
        button.disabled = true;
        try {
          const reason = await confirmAction("Retry live delivery?", `Deliver booking #${b.id} now if the character is ready online.`, "Retry delivery");
          if (reason === null) return;
          const result = await api(`/api/bookings/${b.id}/deliver`, {reason});
          notice(result.status === "delivered" ? "Booking delivered." : result.error || "Character is offline or changing maps. Booking remains queued.", result.status !== "delivered");
          window.dispatchEvent(new Event("reload-page"));
        } catch (error) { formError(error); } finally { button.disabled = false; }
      });
      $("#cancel-form").addEventListener("submit", async event => {
        event.preventDefault(); const button = event.target.querySelector('button[type="submit"]'); button.disabled = true;
        try {
          const reason = await confirmAction("Cancel this booking?", `Booking #${b.id} will no longer be delivered.`, "Cancel booking");
          if (reason === null) return;
          await api(`/api/bookings/${b.id}/cancel`, {reason});
          notice("Booking cancelled."); window.dispatchEvent(new Event("reload-page"));
        } catch (error) { formError(error); } finally { button.disabled = false; }
      });
    };
  }
  result.html = isLegacyInterface()
    ? `<div class="columns">${grant}${delivery}</div>${equipment}${controls}`
    : `<div class="detail-workspace">
        <div>${grant}${equipment}</div>
        <aside aria-label="Delivery and booking actions">${delivery}${controls}</aside>
      </div>`;
  return result;
}

export async function auditPage(route) {
  const a = await api(`/api/audit/${route.parts[1]}`);
  const details = a.details || {};
  const metadata = [["Administrator", esc(a.actor)], ["Time", esc(date(a.created_at))], ["Action", esc(a.action)], ["Target", esc(a.target)]];
  const changes = section("Recorded changes", tree(details));
  const html = isLegacyInterface()
    ? `<div class="columns">${section("Action", facts(metadata))}${changes}</div>`
    : `<div class="audit-workspace">
        <aside aria-label="Audit summary">${section("Action", facts([...metadata, ["Reason", esc(details.reason || "—")]]))}</aside>
        <div>${changes}</div>
      </div>`;
  return {
    title: `Audit entry #${a.id}`, description: a.action,
    breadcrumbs: [["Audit log", "audit"], [`#${a.id}`]], html
  };
}
