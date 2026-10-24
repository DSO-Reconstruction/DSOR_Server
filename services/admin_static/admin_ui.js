"use strict";

export const $ = selector => document.querySelector(selector);
export const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[c]));
export const human = value => String(value).replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
export const date = value => value ? new Date(typeof value === "number" ? value * 1000 : value).toLocaleString() : "—";
export const classes = ["Dragonknight", "Spellweaver", "Ranger", "Steam Mechanicus", "Niwalk"];
export const rarities = ["Common", "Uncommon", "Magic", "Rare", "Epic", "Unique", "Set", "Mythic"];
let csrf = "";
export function setSession(value) { csrf = value; }
export function hasSession() { return !!csrf; }
export function notice(message, error = false) {
  $("#notice").textContent = message;
  $("#notice").className = error ? "notice error" : "notice";
  $("#notice").hidden = false;
  $("#notice").dataset.shown = "0";
}
export async function api(path, data) {
  const options = {credentials: "same-origin", headers: {}};
  if (data !== undefined) {
    options.method = "POST";
    options.headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf};
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") window.dispatchEvent(new Event("signed-out"));
    throw new Error(body.error || "Request failed");
  }
  return body;
}
export function href(path, params = {}) {
  const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null));
  return "#" + path + (query.size ? "?" + query : "");
}
export function link(label, path, params = {}, className = "") {
  return `<a class="${className}" href="${esc(href(path, params))}">${esc(label)}</a>`;
}
export function navigate(path, params) { location.hash = href(path, params); }
export function routeInfo() {
  const raw = location.hash.slice(1) || "status";
  const [path, query = ""] = raw.split("?");
  return {path, parts: path.split("/"), params: Object.fromEntries(new URLSearchParams(query))};
}
export function status(label, tone = "") { return `<span class="status ${tone}">${esc(label)}</span>`; }
export function table(headers, rows, empty = "No records found.") {
  if (!rows.length) return `<div class="empty">${esc(empty)}</div>`;
  return `<div class="table-wrap"><table><thead><tr>${headers.map(v => `<th>${esc(v)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(row => `<tr>${row.map(v => `<td>${v ?? "—"}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
export function facts(rows) {
  return `<dl class="facts">${rows.map(([name, value]) => `<div class="fact"><dt>${esc(name)}</dt><dd>${value ?? "—"}</dd></div>`).join("")}</dl>`;
}
export function stats(rows) {
  return `<div class="stats">${rows.map(([label, value]) => `<div class="stat"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("")}</div>`;
}
export function section(title, html, extra = "") {
  return `<section class="section"><div class="section-heading"><h2>${esc(title)}</h2>${extra}</div>${html}</section>`;
}
export function tabs(entries, active) {
  return `<nav class="tabs" aria-label="Page sections">${entries.map(([label, path, params = {}]) => link(label, path, params, path === active ? "active" : "")).join("")}</nav>`;
}
export function toolbar(route, placeholder, extra = "") {
  return `<form id="search-form" class="toolbar"><input name="q" aria-label="Search" placeholder="${esc(placeholder)}" value="${esc(route.params.q || "")}" maxlength="100"><button class="secondary">Search</button>${extra}</form>`;
}
export function bindSearch(route) {
  $("#search-form")?.addEventListener("submit", event => {
    event.preventDefault();
    navigate(route.path, {...route.params, q: new FormData(event.target).get("q"), offset: 0});
  });
}
export function pager(data, route) {
  return `<div class="pager"><span>${data.total ? data.offset + 1 : 0}–${Math.min(data.offset + data.limit, data.total)} of ${data.total}</span>
    ${data.offset > 0 ? link("Previous", route.path, {...route.params, offset: Math.max(0, data.offset - data.limit)}) : ""}
    ${data.offset + data.limit < data.total ? link("Next", route.path, {...route.params, offset: data.offset + data.limit}) : ""}</div>`;
}
export function formError(error) {
  const target = $("#form-error");
  if (target) { target.textContent = error.message; target.scrollIntoView({block: "nearest"}); }
  else notice(error.message, true);
}
// Every audited action is confirmed in this dialog, which also asks for the
// audit reason. Resolves to the trimmed reason, or null when cancelled.
export function confirmAction(title, message, label = "Confirm") {
  const dialog = $("#confirmation"), reason = $("#confirmation-reason");
  $("#confirmation-title").textContent = title;
  $("#confirmation-message").textContent = message;
  $("#confirmation-accept").textContent = label;
  $("#confirmation-error").textContent = "";
  reason.value = "";
  dialog.returnValue = "";
  dialog.showModal();
  reason.focus();
  return new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirmed" ? reason.value.trim() : null), {once: true});
  });
}
export function bindConfirmation() {
  const dialog = $("#confirmation"), form = $("#confirmation-form"), reason = $("#confirmation-reason");
  $("#confirmation-cancel").addEventListener("click", () => dialog.close());
  form.addEventListener("submit", event => {
    event.preventDefault();
    const length = reason.value.trim().length;
    if (length < 3 || length > 500) {
      $("#confirmation-error").textContent = "Enter a reason of 3 to 500 characters.";
      reason.focus();
      return;
    }
    dialog.close("confirmed");
  });
  reason.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); form.requestSubmit(); }
  });
  reason.addEventListener("input", () => { $("#confirmation-error").textContent = ""; });
}
export function valueView(value) {
  if (value == null || value === "") return '<span class="muted">—</span>';
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value) && value.every(v => v == null || typeof v !== "object")) return esc(value.join(", ") || "None");
  if (typeof value !== "object") return esc(value);
  return tree(value);
}
export function tree(value) {
  const entries = Object.entries(value || {});
  if (!entries.length) return '<p class="muted">None recorded.</p>';
  const leaves = entries.filter(([, v]) => v == null || typeof v !== "object" || (Array.isArray(v) && v.every(x => typeof x !== "object")));
  const branches = entries.filter(entry => !leaves.includes(entry));
  return `<div class="tree">${leaves.length ? facts(leaves.map(([k, v]) => [human(k), valueView(v)])) : ""}${branches.map(([k, v]) =>
    `<details><summary>${esc(human(k))} <small>(${Object.keys(v).length})</small></summary>${tree(v)}</details>`).join("")}</div>`;
}
export function characterTable(rows, ownerColumn = false) {
  return table(["Character", ...(ownerColumn ? ["Account"] : []), "Class", "Level", "Map", "Presence", "Last saved"], rows.map(c => [
    link(c.name, `characters/${c.character_id}`) + `<span class="sub">${c.character_id}</span>`,
    ...(ownerColumn ? [esc(c.owner || "Unclaimed")] : []), esc(classes[c.class_id] || c.class_id), esc(c.level),
    `<code class="resource">${esc(c.map_id)}</code>`, status(c.online ? "Online" : "Offline", c.online ? "good" : ""), esc(date(c.saved_at))
  ]), "No characters are tied to this account.");
}
export function bookingTable(rows) {
  return table(["Booking", "Character", "Resource", "Quantity", "State", "Created", "By"], rows.map(b => [
    link("#" + b.id, `bookings/${b.id}`), link(b.character_id, `characters/${b.character_id}`),
    `<code class="resource">${esc(b.resource)}</code><span class="sub">${esc(b.reason)}</span>`, esc(b.amount),
    status(b.error ? "Pending · needs attention" : human(b.status), b.error ? "bad" : b.status === "delivered" ? "good" : ""),
    esc(date(b.created_at)), esc(b.actor)
  ]));
}
