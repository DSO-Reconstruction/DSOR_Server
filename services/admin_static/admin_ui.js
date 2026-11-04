"use strict";

export const $ = selector => document.querySelector(selector);
export const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[c]));
export const human = value => String(value).replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
export const date = value => value ? new Date(typeof value === "number" ? value * 1000 : value).toLocaleString() : "—";
export const classes = ["Dragonknight", "Spellweaver", "Ranger", "Steam Mechanicus", "Niwalk"];
export const rarities = ["Common", "Uncommon", "Magic", "Rare", "Epic", "Unique", "Set", "Mythic"];
let csrf = "", searchTimer, sectionSpy;
export function setSession(value) { csrf = value; }
export function hasSession() { return !!csrf; }
export function debounce(action, wait = 250) {
  let timer;
  return (...values) => { clearTimeout(timer); timer = setTimeout(() => action(...values), wait); };
}
export function announce(message) { $("#live-status").textContent = message; }
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
// Keeps a filter in the address bar without another render or history entry.
export function rememberParams(values) {
  const {path, params} = routeInfo();
  history.replaceState(null, "", href(path, {...params, ...values}));
}
export function status(label, tone = "") { return `<span class="status ${tone}">${esc(label)}</span>`; }
export function table(headers, rows, empty = "No records found.") {
  if (!rows.length) return `<div class="empty">${esc(empty)}</div>`;
  // Long tables scroll inside their own box, which needs a keyboard stop.
  const scroller = rows.length > 12 ? ` tabindex="0" role="region" aria-label="${esc(headers.find(Boolean) || "Table")} list, scrollable"` : "";
  return `<div class="table-wrap"${scroller}><table><thead><tr>${headers.map(v => `<th scope="col">${esc(v)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(row => `<tr>${row.map(v => `<td>${v ?? "—"}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
// Modern collection views use one readable record per card; the same row data
// still feeds the compact table in classic mode.
export function recordCards(headers, rows, empty = "No records found.") {
  if (!rows.length) return `<div class="empty">${esc(empty)}</div>`;
  const hasActions = headers.length > 1 && ["", "Action", "Actions"].includes(headers.at(-1));
  const stateIndex = headers.findIndex(label => label === "State" || label === "Presence");
  const fields = headers.map((label, index) => ({label, index}))
    .filter(({index}) => index !== 0 && index !== stateIndex && !(hasActions && index === headers.length - 1));
  return `<div class="record-grid">${rows.map(row => `<article class="record-card">
    <header class="record-card-title">
      <div data-record-value="0">${row[0] ?? "—"}</div>
      ${stateIndex > 0 ? `<div class="record-card-state" data-record-value="${stateIndex}">${row[stateIndex] ?? "—"}</div>` : ""}
    </header>
    ${fields.length ? `<dl>${fields.map(({label, index}) => `<div><dt>${esc(label)}</dt><dd data-record-value="${index}">${row[index] ?? "—"}</dd></div>`).join("")}</dl>` : ""}
    ${hasActions ? `<footer class="record-card-actions" data-record-value="${headers.length - 1}"${row.at(-1) ? "" : " hidden"}>${row.at(-1) ?? ""}</footer>` : ""}
  </article>`).join("")}</div>`;
}
export function collectionRecords(headers, rows, empty = "No records found.") {
  return isLegacyInterface() ? table(headers, rows, empty) : recordCards(headers, rows, empty);
}
// A table whose rows are all in the page, with an instant filter above it.
// facets are [label, column, url parameter]: a select of that column's values.
// bindFilters wires these up after every render.
export function filteredTable(id, label, headers, rows, {facets = [], empty, cards = false} = {}) {
  if (!rows.length) return table(headers, rows, empty);
  return `<div class="filtered" data-param="${esc(id)}"><div class="toolbar" role="search" aria-label="${esc(label)}">
      <input id="filter-${esc(id)}" type="search" aria-label="${esc(label)}" placeholder="${esc(label)}" maxlength="100" autocomplete="off" spellcheck="false">
      ${facets.map(([name, column, param]) => `<select id="filter-${esc(param)}" aria-label="${esc(name)}" data-name="${esc(name)}" data-column="${column}" data-param="${esc(param)}"></select>`).join("")}
      <span class="spacer"></span><span class="list-count"></span></div>
    ${cards ? collectionRecords(headers, rows) : table(headers, rows)}
    <div class="empty" hidden>Nothing matches this filter. <button type="button" class="text-button">Clear filter</button></div></div>`;
}
export function bindFilters(root) {
  for (const box of root.querySelectorAll(".filtered")) {
    const input = box.querySelector("input"), selects = [...box.querySelectorAll("select")];
    const count = box.querySelector(".list-count"), none = box.querySelector(":scope > .empty");
    const grid = box.querySelector(":scope > .record-grid");
    const rows = grid ? [...grid.children] : [...box.querySelector("tbody").rows];
    const values = rows.map(row => grid
      ? [...row.querySelectorAll("[data-record-value]")]
          .sort((a, b) => Number(a.dataset.recordValue) - Number(b.dataset.recordValue))
          .map(cell => cell.textContent.trim())
      : [...row.cells].map(cell => cell.textContent.trim()));
    const text = values.map(cells => cells.join(" ").toLowerCase());
    const cell = (index, select) => values[index][select.dataset.column];
    const {params} = routeInfo();
    for (const select of selects) {
      const counts = Object.create(null);
      for (let index = 0; index < rows.length; index++) {
        const value = cell(index, select);
        counts[value] = (counts[value] || 0) + 1;
      }
      select.innerHTML = `<option value="">${esc(select.dataset.name)}: all (${rows.length})</option>` + Object.keys(counts).sort()
        .map(value => `<option value="${esc(value)}">${esc(value)} (${counts[value]})</option>`).join("");
      select.value = params[select.dataset.param] || "";
    }
    input.value = params[box.dataset.param] || "";
    const announceCount = debounce(announce, 400);
    const apply = () => {
      const terms = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      rows.forEach((row, index) => {
        const match = terms.every(term => text[index].includes(term)) && selects.every(select => !select.value || cell(index, select) === select.value);
        row.hidden = !match;
        if (match) shown++;
      });
      const filtering = terms.length || selects.some(select => select.value);
      count.textContent = filtering ? `${shown} of ${rows.length}` : `${rows.length} total`;
      (grid || box.querySelector(".table-wrap")).hidden = !shown;
      none.hidden = !!shown;
      rememberParams({[box.dataset.param]: input.value, ...Object.fromEntries(selects.map(select => [select.dataset.param, select.value]))});
      return filtering && `${shown} of ${rows.length} shown`;
    };
    const update = () => { const message = apply(); if (message) announceCount(message); };
    input.addEventListener("input", update);
    input.addEventListener("keydown", event => {
      if (event.key === "Escape" && input.value) { event.preventDefault(); input.value = ""; update(); }
    });
    selects.forEach(select => select.addEventListener("change", update));
    none.querySelector("button").addEventListener("click", () => {
      input.value = ""; selects.forEach(select => { select.value = ""; });
      update(); input.focus();
    });
    apply();
  }
}
// Long pages get a sticky index of their sections. The fragment belongs to the
// router, so these links scroll and move focus themselves.
export function sectionNav(route) {
  sectionSpy?.();
  sectionSpy = null;
  const content = $("#content"), headings = [...content.querySelectorAll(".section-heading h2")];
  const slug = heading => heading.textContent.toLowerCase().replace(/[^a-z0-9]+/g, "-");
  headings.forEach(heading => { heading.id = "section-" + slug(heading); heading.tabIndex = -1; });
  const jump = id => {
    const heading = document.getElementById(id);
    heading?.scrollIntoView({block: "start"});
    heading?.focus({preventScroll: true});
  };
  if (headings.length >= 3 && document.documentElement.scrollHeight > innerHeight * 1.3) {
    const nav = document.createElement("nav");
    nav.className = "section-nav";
    nav.setAttribute("aria-label", "On this page");
    nav.innerHTML = headings.map(heading => `<a href="${esc(href(route.path, {...route.params, section: slug(heading)}))}" data-section="${heading.id}">${esc(heading.textContent)}</a>`).join("");
    const pageTabs = content.querySelector(":scope > .tabs");
    if (pageTabs) pageTabs.after(nav); else content.prepend(nav);
    nav.addEventListener("click", event => {
      const anchor = event.target.closest("a");
      if (!anchor || event.ctrlKey || event.metaKey || event.shiftKey) return;
      event.preventDefault();
      rememberParams({section: anchor.dataset.section.slice("section-".length)});
      jump(anchor.dataset.section);
    });
    // The current section is the lowest heading past the sticky bars; the first
    // of two side-by-side sections wins, and the page end selects the last one.
    const mark = () => {
      const tops = headings.map(heading => heading.getBoundingClientRect().top);
      const ended = scrollY + innerHeight >= document.documentElement.scrollHeight - 2;
      const readingLine = parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) || 150;
      const current = ended ? headings.length - 1 : tops.reduce((best, top, index) => top <= readingLine && top > tops[best] + 1 ? index : best, 0);
      [...nav.children].forEach((anchor, index) => {
        if (index === current) anchor.setAttribute("aria-current", "location"); else anchor.removeAttribute("aria-current");
      });
    };
    window.addEventListener("scroll", mark, {passive: true});
    sectionSpy = () => window.removeEventListener("scroll", mark);
    mark();
  }
  if (route.params.section) jump("section-" + route.params.section);
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
const interfaceKey = accountId => `dso-admin-ui-mode:${accountId}`;

export function readInterfacePreference(accountId) {
  if (accountId == null) return "modern";
  try { return localStorage.getItem(interfaceKey(accountId)) === "legacy" ? "legacy" : "modern"; }
  catch { return "modern"; }
}

export function saveInterfacePreference(mode) {
  localStorage.setItem(interfaceKey(window.dsoAdminAccountId), mode);
}

export function isLegacyInterface() {
  return document.documentElement.dataset.interface === "legacy";
}

export function collectionPanel(title, content, footer = "", count = null, description = "Browse and manage CMS records.") {
  if (isLegacyInterface()) return content + footer;
  const countMarkup = count == null ? "" : `<span class="collection-count"><strong>${esc(Number(count).toLocaleString())}</strong><small>records</small></span>`;
  return `<section class="collection-panel" aria-label="${esc(title)} records">
    <header class="collection-panel-head"><div><h2>${esc(title)}</h2><p>${esc(description)}</p></div>${countMarkup}</header>
    ${content}${footer ? `<footer class="collection-panel-foot">${footer}</footer>` : ""}</section>`;
}
export function collectionWorkspace(tools, panel) {
  return isLegacyInterface() ? tools + panel : `<div class="collection-workspace"><div class="collection-tools">${tools}</div>${panel}</div>`;
}
export function tabs(entries, active) {
  return `<nav class="tabs" aria-label="Page sections">${entries.map(([label, path, params = {}, count]) =>
    `<a class="${path === active ? "active" : ""}" href="${esc(href(path, params))}"${path === active ? ' aria-current="page"' : ""}>${esc(label)}${count == null ? "" : ` <small>${esc(Number(count).toLocaleString())}</small>`}</a>`).join("")}</nav>`;
}
export function toolbar(route, placeholder, extra = "") {
  return `<form id="search-form" class="toolbar" role="search"><input type="search" name="q" aria-label="${esc(placeholder)}" placeholder="${esc(placeholder)}" value="${esc(route.params.q || "")}" maxlength="100" autocomplete="off"><button class="secondary">Search</button>${extra}</form>`;
}
// Server-side lists search as you type. Replacing the history entry keeps Back
// on the previous page instead of the previous keystroke.
export function bindSearch(route) {
  const form = $("#search-form");
  if (!form) return;
  const search = form.elements.q;
  const apply = () => {
    clearTimeout(searchTimer);
    const q = search.value.trim(), {params} = routeInfo();
    if (q !== (params.q || "")) location.replace(href(route.path, {...params, q, offset: ""}));
  };
  form.addEventListener("submit", event => { event.preventDefault(); apply(); });
  search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(apply, 300); });
  search.addEventListener("keydown", event => {
    if (event.key === "Escape" && search.value) { event.preventDefault(); search.value = ""; apply(); }
  });
}
// The find-as-you-type behaviour of the item, account and map pickers.
export function searchAsYouType(input, action, results) {
  input.addEventListener("input", debounce(async () => {
    await action();
    const count = results.querySelectorAll("tbody tr").length;
    if (input.isConnected) announce(`${count} ${count === 1 ? "result" : "results"}`);
  }));
}
export function pager(data, route) {
  const {total, offset, limit} = data, pages = Math.ceil(total / limit), current = Math.floor(offset / limit) + 1;
  const go = (label, page, show) => show ? `<a id="pager-${label.toLowerCase()}" href="${esc(href(route.path, {...route.params, offset: (page - 1) * limit || ""}))}">${label}</a>` : "";
  return `<nav class="pager" aria-label="Pagination"><span data-list-count>${total ? `${offset + 1}–${Math.min(offset + limit, total)} of ${total.toLocaleString()}` : "No results"}</span>
    ${go("First", 1, current > 2)}${go("Previous", current - 1, current > 1)}
    ${pages > 2 ? `<label>Page <input id="pager-page" type="number" min="1" max="${pages}" step="1" value="${current}" data-page-limit="${limit}" aria-label="Page number, of ${pages}"></label><span>of ${pages.toLocaleString()}</span>` : ""}
    ${go("Next", current + 1, current < pages)}${go("Last", pages, current < pages - 1)}</nav>`;
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
  if (Array.isArray(value) && value.every(v => v == null || typeof v !== "object")) return valueList(value);
  if (typeof value !== "object") return esc(value);
  return tree(value);
}
export function valueList(values) {
  if (values.length <= 6) return esc(values.join(", ") || "None");
  return `<details class="value-list"><summary>${values.length} values</summary><ul>${values.map(v => `<li>${esc(v)}</li>`).join("")}</ul></details>`;
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
  return table(characterHeaders(ownerColumn), characterRows(rows, ownerColumn), "No characters are tied to this account.");
}
export function characterHeaders(ownerColumn = false) { return ["Character", ...(ownerColumn ? ["Account"] : []), "Class", "Level", "Map", "Presence", "Last saved"]; }
export function characterRows(rows, ownerColumn = false) { return rows.map(c => [
    link(c.name, `characters/${c.character_id}`) + `<span class="sub">${c.character_id}</span>`,
    ...(ownerColumn ? [esc(c.owner || "Unclaimed")] : []), esc(classes[c.class_id] || c.class_id), esc(c.level),
    `<code class="resource">${esc(c.map_id)}</code>`, status(c.online ? "Online" : "Offline", c.online ? "good" : ""), esc(date(c.saved_at))
  ]); }
export function bookingGrants(booking) {
  if (booking.kind !== "batch") return `<code class="resource">${esc(booking.resource)}</code>`;
  return JSON.parse(booking.resource).map(grant => `${esc(grant.kind === "currency" ? human(grant.resource) : grant.resource)} × ${esc(grant.amount)}`).join("<br>");
}
export function bookingTable(rows) {
  return table(bookingHeaders(), bookingRows(rows));
}
export function bookingHeaders() { return ["Booking", "Character", "Resource", "Quantity", "State", "Created", "By"]; }
export function bookingRows(rows) { return rows.map(b => [
    link("#" + b.id, `bookings/${b.id}`), link(b.character_id, `characters/${b.character_id}`),
    `${bookingGrants(b)}<span class="sub">${esc(b.reason)}</span>`, esc(b.amount),
    status(b.error ? "Pending · needs attention" : human(b.status), b.error ? "bad" : b.status === "delivered" ? "good" : ""),
    esc(date(b.created_at)), esc(b.actor)
  ]); }

export function mapWorkspace(directory, editor) {
  if (isLegacyInterface()) return directory + editor;
  return `<div class="map-workspace">
    <aside aria-label="Map directory">${directory}</aside>
    <div class="map-workspace-editor">${editor}</div>
  </div>`;
}
