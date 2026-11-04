import { $, esc, human, date, api, link, navigate, status, table, collectionRecords, isLegacyInterface, facts, section, toolbar, bindSearch, pager, collectionPanel, collectionWorkspace, formError, notice, confirmAction } from "./admin_ui.js";
import { grantEditor, mountGrantEditor } from "./admin_booking.js";

// No I, O, 0 or 1. Thirty-two symbols, so a random byte maps without bias.
const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const randomCode = () => Array.from(crypto.getRandomValues(new Uint8Array(12)),
  (value, index) => (index && index % 4 === 0 ? "-" : "") + alphabet[value % alphabet.length]).join("");
const states = {active: ["Active", "good"], disabled: ["Disabled", ""], expired: ["Expired", ""], exhausted: ["Used up", ""]};
const stateLabel = code => status(...states[code.state]);
const redemptions = code => `${code.redemptions.toLocaleString()} of ${code.max_redemptions ? code.max_redemptions.toLocaleString() : "unlimited"}`;
const reward = code => code.grants.map(grant => `${esc(grant.kind === "currency" ? human(grant.resource) : grant.name)} × ${esc(Number(grant.amount).toLocaleString())}`).join("<br>");

// datetime-local works in local time without a zone, the API in epoch seconds.
function localTime(seconds) {
  if (!seconds) return "";
  const value = new Date(seconds * 1000);
  value.setMinutes(value.getMinutes() - value.getTimezoneOffset());
  return value.toISOString().slice(0, 16);
}
const epochSeconds = value => value ? Math.floor(new Date(value).getTime() / 1000) : null;

const limitFields = (code = {}) => `<div class="form-grid">
    <label>Total redemptions<input name="max_redemptions" type="number" min="0" max="1000000000" step="1" value="${code.max_redemptions || ""}" placeholder="Unlimited"><small>Across all accounts. Leave blank for no limit.</small></label>
    <label>Expires<input name="expires_at" type="datetime-local" value="${esc(localTime(code.expires_at))}"><small>Leave blank to keep the code valid until you disable it.</small></label>
    <label>Note<input name="note" maxlength="200" value="${esc(code.note || "")}"><small>Only shown here.</small></label>
  </div>`;
const limitValues = form => {
  const values = new FormData(form);
  return {max_redemptions: Number(values.get("max_redemptions") || 0), expires_at: epochSeconds(values.get("expires_at")), note: values.get("note")};
};

export async function bonusCodesPage(route) {
  const data = await api(`/api/bonus-codes?${new URLSearchParams({limit: 50, ...route.params})}`);
  return {
    title: "Bonus codes", description: "Codes players redeem under Redeem voucher in the shop. Each one books a fixed reward to the character that redeems it.",
    breadcrumbs: [["Administration", "status"], ["Bonus codes"]],
    actions: link("New bonus code", "bonus-codes/new", {}, "button"),
    html: collectionWorkspace(toolbar(route, "Search codes and notes"), collectionPanel("Voucher registry",
      collectionRecords(["Code", "Reward", "Redeemed", "State", "Expires", "Note", "Actions"], data.rows.map(code => [
        link(code.code, `bonus-codes/${code.id}`, {}, "mono"), reward(code), esc(redemptions(code)), stateLabel(code),
        code.expires_at ? esc(date(code.expires_at)) : "Never", esc(code.note),
        `<button type="button" class="${code.enabled ? "danger" : "secondary"} bonus-code-toggle" data-toggle-bonus-code="${code.id}" data-enabled="${code.enabled}" aria-label="${code.enabled ? "Disable" : "Enable"} bonus code ${esc(code.code)}">${code.enabled ? "Disable" : "Enable"}</button>`
      ]), "No bonus codes yet."), pager(data, route), data.total, "Rewards redeemed by players in the shop.")),
    mount() {
      bindSearch(route);
      $("#content").addEventListener("click", async event => {
        const button = event.target.closest("[data-toggle-bonus-code]");
        if (!button || !button.isConnected) return;
        const enabled = button.dataset.enabled === "true";
        const code = data.rows.find(row => String(row.id) === button.dataset.toggleBonusCode);
        if (!code) return;
        button.disabled = true;
        try {
          const reason = await confirmAction(enabled ? "Disable this code?" : "Enable this code?",
            enabled ? `${code.code} stops working. Rewards already redeemed are not affected.` : `${code.code} can be redeemed again.`,
            enabled ? "Disable code" : "Enable code");
          if (reason === null) return;
          await api(`/api/bonus-codes/${code.id}`, {changes: {enabled: !enabled}, reason});
          notice(`Bonus code ${code.code} ${enabled ? "disabled" : "enabled"}.`);
          window.dispatchEvent(new Event("reload-page"));
        } catch (error) { notice(error.message, true); }
        finally { if (button.isConnected) button.disabled = false; }
      });
    }
  };
}

export async function newBonusCodePage() {
  const currencies = (await api("/api/currencies")).currencies;
  return {
    title: "New bonus code", description: "Choose the code and the reward. Players enter it under Redeem voucher in the shop.",
    breadcrumbs: [["Bonus codes", "bonus-codes"], ["New bonus code"]],
    html: `<div class="form-page"><form id="bonus-code-form">
      <fieldset class="form-section"><legend>1. Code</legend>
        <label for="bonus-code">Code</label>
        <div class="toolbar"><input id="bonus-code" name="code" required minlength="3" maxlength="32" pattern="[A-Za-z0-9][A-Za-z0-9_\\-]*" autocomplete="off" spellcheck="false"><button id="generate-code" type="button" class="secondary">Generate</button></div>
        <p class="help">3 to 32 letters, digits, hyphens or underscores. Players can type it in upper or lower case.</p>
      </fieldset>
      <fieldset class="form-section"><legend>2. Reward</legend>${grantEditor(currencies)}</fieldset>
      <fieldset class="form-section"><legend>3. Limits</legend>${limitFields()}</fieldset>
      <p class="help">Each account can redeem a code once, on any of its characters. The reward goes to that character as a booking: immediately when it is ready, otherwise at its next map entry.</p>
      <p id="form-error" class="error-text" role="alert"></p>
      <div class="form-actions">${link("Cancel", "bonus-codes", {}, "left")}<button type="submit">Create bonus code</button></div>
    </form></div>`,
    mount() {
      const form = $("#bonus-code-form"), grants = mountGrantEditor(form, currencies);
      $("#generate-code").addEventListener("click", () => { form.elements.code.value = randomCode(); });
      form.addEventListener("submit", async event => {
        event.preventDefault();
        const button = form.querySelector('button[type="submit"]'); button.disabled = true;
        try {
          if (!grants.length) throw new Error("Add at least one item or currency.");
          const code = form.elements.code.value.trim().toUpperCase();
          const reason = await confirmAction("Create this bonus code?", `${code} grants ${grants.length} ${grants.length === 1 ? "reward" : "rewards"} to each account that redeems it.`, "Create code");
          if (reason === null) return;
          const created = await api("/api/bonus-codes", {code, ...limitValues(form), reason,
            items: grants.map(({kind, resource, amount, options}) => ({kind, resource, amount, options}))});
          notice(`Bonus code ${created.code} created.`);
          navigate(`bonus-codes/${created.id}`);
        } catch (error) { if (form.isConnected) formError(error); }
        finally { button.disabled = false; }
      });
    }
  };
}

export async function bonusCodePage(route) {
  const code = await api(`/api/bonus-codes/${route.parts[1]}?${new URLSearchParams({limit: 50, ...route.params})}`);
  const save = async (button, title, message, label, changes, done) => {
    button.disabled = true;
    try {
      const reason = await confirmAction(title, message, label);
      if (reason === null) return;
      await api(`/api/bonus-codes/${code.id}`, {changes: changes(), reason});
      notice(done);
      window.dispatchEvent(new Event("reload-page"));
    } catch (error) { formError(error); } finally { button.disabled = false; }
  };
  const overview = `<div class="columns">${section("Reward", facts(code.grants.map(grant => [grant.kind === "currency" ? "Currency" : "Item",
        `${esc(grant.kind === "currency" ? human(grant.resource) : grant.name)} × ${esc(Number(grant.amount).toLocaleString())}${grant.kind === "item" ? `<span class="sub mono">${esc(grant.resource)}</span>` : ""}`])))}
      ${section("Status", facts([["State", stateLabel(code)], ["Redeemed", esc(redemptions(code))],
        ["Expires", code.expires_at ? esc(date(code.expires_at)) : "Never"], ["Created", esc(date(code.created_at))], ["Administrator", esc(code.actor)]]))}</div>`;
  const limits = section("Limits", `<form id="bonus-code-limits" class="form-page">${limitFields(code)}
        <p id="form-error" class="error-text" role="alert"></p>
        <div class="form-actions"><button id="toggle-code" type="button" class="secondary">${code.enabled ? "Disable code" : "Enable code"}</button><button type="submit">Save limits</button></div></form>`);
  const history = section("Redemptions", table(["Redeemed", "Account", "Character", "Booking", "Delivery"], code.history.rows.map(row => [
        esc(date(row.created_at)), link(row.username || row.account_id, `accounts/${row.account_id}`),
        link(row.character_name || row.character_id, `characters/${row.character_id}`), link("#" + row.booking_id, `bookings/${row.booking_id}`),
        status(row.booking_error ? "Pending · needs attention" : human(row.booking_status), row.booking_error ? "bad" : row.booking_status === "delivered" ? "good" : "")
      ]), "Nobody has redeemed this code yet.") + pager(code.history, route));
  const layout = isLegacyInterface() ? overview + limits + history
    : `<div class="voucher-workspace">
        <div class="voucher-workspace-summary">${overview}</div>
        <div class="voucher-workspace-limits">${limits}</div>
        <div class="voucher-workspace-history">${history}</div>
      </div>`;
  return {
    title: code.code, description: `${code.grants.length} ${code.grants.length === 1 ? "reward" : "rewards"} · ${code.max_redemptions === 1 ? "Single use" : redemptions(code) + " redeemed"}`,
    breadcrumbs: [["Bonus codes", "bonus-codes"], [code.code]],
    actions: link("New bonus code", "bonus-codes/new", {}, "button secondary"),
    html: layout,
    mount() {
      const form = $("#bonus-code-limits");
      form.addEventListener("submit", event => {
        event.preventDefault();
        save(form.querySelector('button[type="submit"]'), "Save these limits?", `The new limits apply to ${code.code} from now on. Earlier redemptions stay as they are.`, "Save limits", () => limitValues(form), "Limits saved.");
      });
      $("#toggle-code").addEventListener("click", event => {
        save(event.target, code.enabled ? "Disable this code?" : "Enable this code?",
          code.enabled ? `${code.code} stops working. Rewards already redeemed are not affected.` : `${code.code} can be redeemed again.`,
          code.enabled ? "Disable code" : "Enable code", () => ({enabled: !code.enabled}), code.enabled ? "Bonus code disabled." : "Bonus code enabled.");
      });
    }
  };
}
