import { $, readInterfacePreference, esc, api, setSession, hasSession, routeInfo, link, navigate, notice, announce, bindConfirmation, bindFilters, sectionNav } from "./admin_ui.js";
import { bindQuickFind } from "./admin_find.js";
import { listPage, accountPage, itemPage, bookingPage, auditPage } from "./admin_pages.js";
import { characterPage } from "./admin_character.js";
import { newBookingPage, batchBookingPage } from "./admin_booking.js";
import { eventsPage, eventPage, newEventPage } from "./admin_events.js";
import { npcShopsPage, npcShopPage } from "./admin_shops.js";
import { seasonPassPage } from "./admin_season_pass.js";
import { lootRatesPage } from "./admin_loot.js";
import { dungeonElitesPage } from "./admin_elites.js";
import { mysticCubePage } from "./admin_mystic_cube.js";
import { bonusCodesPage, bonusCodePage, newBonusCodePage } from "./admin_bonus_codes.js";
import { settingsPage, serverControlPage } from "./admin_control.js";

let generation = 0, shownPath = null;
function applyInterface(accountId) {
  const mode = readInterfacePreference(accountId);
  const classic = mode === "legacy";
  const footer = $("#classic-session"), toolbar = $(".global-tools");
  const identity = $(".console-session"), refresh = $("#refresh"), logout = $("#logout");
  footer.hidden = !classic;
  if (classic) {
    footer.append(identity, logout);
    $("#page-actions").after(refresh);
  } else {
    toolbar.append(identity, refresh, logout);
  }
  if (mode === "legacy") {
    $("#admin-style").href = "/admin_legacy.css";
    document.documentElement.dataset.interface = "legacy";
  } else {
    $("#admin-style").href = "/admin.css";
    delete document.documentElement.dataset.interface;
  }
}

function signedOut() {
  generation++;
  shownPath = null;
  setSession("");
  $("#app").hidden = true;
  delete window.dsoAdminAccountId;
  applyInterface(null);
  $("#identity").textContent = "";
  $("#login-view").hidden = false;
  $("#confirmation").close();
  $("#quick-find").close();
  $("#content").replaceChildren();
  $("#refresh").disabled = false;
}
// A search or filter re-render replaces the input being typed in, so carry its
// focus, text and caret over to the new one.
function focusedField() {
  const element = document.activeElement;
  if (!element?.closest("#content")) return null;
  const selector = element.id ? "#" + CSS.escape(element.id) : element.name ? `[name="${CSS.escape(element.name)}"]` : "";
  return {selector, search: element.type === "search", value: element.value, start: element.selectionStart, end: element.selectionEnd};
}
function restoreField(field) {
  if (document.activeElement !== document.body) return;
  const element = field.selector && $("#content").querySelector(field.selector);
  if (!element) return $("#page-title").focus({preventScroll: true});
  if (field.search) { element.value = field.value; element.setSelectionRange(field.start, field.end); }
  element.focus({preventScroll: true});
}
function markCurrentPage(path) {
  const links = [...document.querySelectorAll(".sidebar nav a")];
  for (const link of links) { const label = link.textContent.trim(); link.setAttribute("aria-label", label); link.title = label; }
  const current = links.filter(a => path === a.hash.slice(1) || path.startsWith(a.hash.slice(1) + "/"))
    .sort((a, b) => b.hash.length - a.hash.length)[0];
  for (const a of links) {
    a.classList.toggle("active", a === current);
    if (a === current) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}
function signedIn(data) {
  setSession(data.csrf);
  window.dsoAdminAccountId = data.account.account_id;
  applyInterface(data.account.account_id);
  $("#identity").textContent = data.account.username;
  $("#login-view").hidden = true;
  $("#app").hidden = false;
  render();
}
async function render() {
  if (!hasSession()) return;
  const version = ++generation, route = routeInfo();
  $("#refresh").disabled = true;
  $("#content").setAttribute("aria-busy", "true");
  $("#confirmation").close();
  if ($("#notice").classList.contains("error") || $("#notice").dataset.shown === "1") {
    $("#notice").hidden = true;
  } else {
    $("#notice").dataset.shown = "1";
  }
  try {
    let result;
    const [name, id] = route.parts;
    if (name === "server-control" && !id) result = await serverControlPage();
    else if (name === "settings" && !id) result = settingsPage();
    else if (name === "npc-shops") result = id ? await npcShopPage(route) : await npcShopsPage(route);
    else if (name === "events") result = id === "new" ? await newEventPage(route) : id ? await eventPage(route) : await eventsPage(route);
    else if (name === "season-pass") result = await seasonPassPage(route);
    else if (name === "loot-rates") result = await lootRatesPage(route);
    else if (name === "dungeon-elites") result = await dungeonElitesPage(route);
    else if (name === "mystic-cube") result = await mysticCubePage(route);
    else if (name === "bonus-codes") result = id === "new" ? await newBonusCodePage(route) : id ? await bonusCodePage(route) : await bonusCodesPage(route);
    else if (name === "bookings" && id === "new") result = await newBookingPage(route);
    else if (name === "bookings" && id === "batch") result = await batchBookingPage(route);
    else if (name === "accounts" && id) result = await accountPage(route);
    else if (name === "characters" && id) result = await characterPage(route);
    else if (name === "items" && id) result = await itemPage(route);
    else if (name === "bookings" && id) result = await bookingPage(route);
    else if (name === "audit" && id) result = await auditPage(route);
    else if (["status", "accounts", "characters", "bookings", "items", "sessions", "audit"].includes(name) && !id) result = await listPage(route);
    else throw new Error("Page not found.");
    if (version !== generation) return;
    $("#page-title").textContent = result.title;
    document.title = result.title + " · DSO Administration";
    $("#page-description").textContent = result.description || "";
    $("#page-actions").innerHTML = result.actions || "";
    $("#breadcrumbs").innerHTML = result.breadcrumbs.map(([label, path, params]) => path ? link(label, path, params) : `<span aria-current="page">${esc(label)}</span>`).join('<span class="separator" aria-hidden="true">/</span>');
    markCurrentPage(route.path);
    const samePage = route.path === shownPath, entered = shownPath !== null, field = samePage ? focusedField() : null;
    $("#content").innerHTML = result.html;
    result.mount?.();
    bindFilters($("#content"));
    sectionNav(route);
    shownPath = route.path;
    if (field) restoreField(field);
    else if (!samePage && entered) $("#page-title").focus({preventScroll: true});
    if (samePage) announce($("#content [data-list-count]")?.textContent || "");
  } catch (error) {
    if (version !== generation) return;
    $("#page-title").textContent = "Unable to load this page";
    $("#page-description").textContent = "";
    $("#page-actions").replaceChildren();
    $("#content").innerHTML = '<p class="muted">Check the address or use the navigation to open another page.</p>';
    notice(error.message, true);
  } finally {
    if (version === generation) {
      $("#refresh").disabled = false;
      $("#content").setAttribute("aria-busy", "false");
    }
  }
}
bindConfirmation();
bindQuickFind();
$("#skip-to-content").addEventListener("click", () => $("#page-title").focus());
$("#content").addEventListener("change", event => {
  const input = event.target.closest("[data-page-limit]");
  if (!input) return;
  const {path, params} = routeInfo(), page = Math.max(1, Math.min(Number(input.max), Math.floor(Number(input.value)) || 1));
  navigate(path, {...params, offset: (page - 1) * Number(input.dataset.pageLimit) || ""});
});
$("#login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.target, button = form.querySelector("button");
  button.disabled = true; $("#login-error").textContent = "";
  try {
    const formData = new FormData(form);
    const data = await api("/api/login", {
      username: formData.get("username"),
      password: formData.get("password"),
      remember: formData.has("remember"),
    });
    form.reset(); signedIn(data);
  }
  catch (error) { $("#login-error").textContent = error.message; }
  finally { button.disabled = false; }
});
$("#logout").addEventListener("click", async () => {
  try { await api("/api/logout", {}); signedOut(); } catch (error) { notice(error.message, true); }
});
$("#refresh").addEventListener("click", render);
window.addEventListener("hashchange", () => { if (routeInfo().path !== shownPath) window.scrollTo(0, 0); render(); });
window.addEventListener("signed-out", signedOut);
window.addEventListener("reload-page", render);
api("/api/me").then(signedIn).catch(signedOut).finally(() => {
  $("#login-form button").disabled = false;
  document.documentElement.dataset.ready = "true";
});
