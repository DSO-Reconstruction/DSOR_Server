import { $, esc, api, classes, debounce, hasSession } from "./admin_ui.js";

const shortcuts = [["Create account", "#accounts/new"], ["New booking", "#bookings/new"], ["Add event", "#events/new"],
  ["Season Pass rewards", "#season-pass/rewards"], ["Season Pass missions", "#season-pass/missions"]];
const sources = [
  ["Characters", "characters", c => [c.name, `#characters/${c.character_id}`, `${classes[c.class_id] ?? c.class_id} · Level ${c.level} · ${c.owner || "Unclaimed"}`]],
  ["Accounts", "accounts", a => [a.username, `#accounts/${a.account_id}`, `Account ${a.account_id}`]],
  ["Items", "items", i => [i.name, `#items/${encodeURIComponent(i.template_id)}`, `${i.template_id} · ${i.category}`]],
  ["NPC shops", "npc-shops", s => [s.shop_id, `#npc-shops/${encodeURIComponent(s.shop_id)}`, `${s.offer_count} offers`]],
  ["Events", "events", e => [e.name, `#events/${e.event_id}`, `${e.template_id} · Instance ${e.event_id}`]]
];

// Results are plain links in a modal dialog: Tab, Enter, new-tab clicks and
// screen reader link lists all work, and the arrow keys are only a shortcut.
export function bindQuickFind() {
  const dialog = $("#quick-find"), input = $("#quick-find-input"), results = $("#quick-find-results"), summary = $("#quick-find-status");
  let generation = 0;
  const named = (selector, text) => [...document.querySelectorAll(selector)].map(a => [a.textContent, a.getAttribute("href")])
    .filter(([label]) => label.toLowerCase().includes(text));
  const local = text => [["On this page", named("#content .tabs a, #content .section-nav a", text)],
    ["Pages", [...named(".sidebar nav a", text), ...shortcuts.filter(([label]) => label.toLowerCase().includes(text))]]];
  function draw(groups, done) {
    const filled = groups.filter(([, rows]) => rows.length), total = filled.reduce((sum, [, rows]) => sum + rows.length, 0);
    results.innerHTML = filled.map(([title, rows]) => `<section><h3>${esc(title)}</h3><ul>${rows.map(([label, target, detail]) =>
      `<li><a href="${esc(target)}">${esc(label)}${detail ? `<small>${esc(detail)}</small>` : ""}</a></li>`).join("")}</ul></section>`).join("") ||
      (done ? '<p class="muted">Nothing found. Try a template ID, character ID or part of a name.</p>' : "");
    if (done) summary.textContent = !total ? "Nothing found" : `${total} ${total === 1 ? "result" : "results"}`;
  }
  const remote = debounce(async (text, version, groups) => {
    const found = await Promise.allSettled(sources.map(([, path]) => api(`/api/${path}?limit=5&q=${encodeURIComponent(text)}`)));
    if (version !== generation || !dialog.open) return;
    draw([...groups, ...sources.map(([title, , row], index) => [title, found[index].status === "fulfilled" ? found[index].value.rows.map(row) : []])], true);
  }, 200);
  function update() {
    const text = input.value.trim().toLowerCase(), version = ++generation, groups = local(text);
    draw(groups, text.length < 2);
    if (text.length >= 2) remote(text, version, groups);
  }
  function open() {
    input.value = "";
    update();
    dialog.showModal();
    input.focus();
  }
  input.addEventListener("input", update);
  $("#open-find").addEventListener("click", open);
  window.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k" && hasSession() && !document.querySelector("dialog[open]")) {
      event.preventDefault();
      open();
    }
  });
  dialog.addEventListener("keydown", event => {
    const links = [...results.querySelectorAll("a")], position = links.indexOf(document.activeElement);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const next = position + (event.key === "ArrowDown" ? 1 : -1);
      (next < 0 ? input : links[Math.min(next, links.length - 1)])?.focus();
    } else if (event.key === "Enter" && document.activeElement === input) {
      event.preventDefault();
      links[0]?.click();
    } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && document.activeElement !== input) input.focus();
  });
  results.addEventListener("click", event => { if (event.target.closest("a")) dialog.close(); });
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
}
