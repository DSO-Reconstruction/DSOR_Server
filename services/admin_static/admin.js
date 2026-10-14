import { $, esc, api, setSession, hasSession, routeInfo, link, notice } from "./admin_ui.js";
import { listPage, accountPage, itemPage, bookingPage, auditPage } from "./admin_pages.js";
import { characterPage } from "./admin_character.js";
import { newBookingPage } from "./admin_booking.js";
import { eventsPage, eventPage, newEventPage } from "./admin_events.js";

let generation = 0;
function signedOut() {
  generation++;
  setSession("");
  $("#app").hidden = true;
  $("#login-view").hidden = false;
  $("#confirmation").close();
  $("#content").replaceChildren();
  $("#refresh").disabled = false;
}
function signedIn(data) {
  setSession(data.csrf);
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
    if (name === "events") result = id === "new" ? await newEventPage(route) : id ? await eventPage(route) : await eventsPage(route);
    else if (name === "bookings" && id === "new") result = await newBookingPage(route);
    else if (name === "accounts" && id) result = await accountPage(route);
    else if (name === "characters" && id) result = await characterPage(route);
    else if (name === "items" && id) result = await itemPage(route);
    else if (name === "bookings" && id) result = await bookingPage(route);
    else if (name === "audit" && id) result = await auditPage(route);
    else if (["status", "accounts", "characters", "bookings", "items", "sessions", "audit"].includes(name) && !id) result = await listPage(route);
    else throw new Error("Page not found.");
    if (version !== generation) return;
    $("#page-title").textContent = result.title;
    document.title = result.title + " ? DSO Administration";
    $("#page-description").textContent = result.description || "";
    $("#page-actions").innerHTML = result.actions || "";
    $("#breadcrumbs").innerHTML = result.breadcrumbs.map(([label, path, params]) => path ? link(label, path, params) : `<span aria-current="page">${esc(label)}</span>`).join('<span class="separator" aria-hidden="true">/</span>');
    document.querySelectorAll(".sidebar nav a").forEach(a => {
      const active = a.hash === "#" + name;
      a.classList.toggle("active", active);
      if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    $("#content").innerHTML = result.html;
    result.mount?.();
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
$("#confirmation-cancel").addEventListener("click", () => $("#confirmation").close());
$("#confirmation-accept").addEventListener("click", () => $("#confirmation").close("confirmed"));
$("#login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.target, button = form.querySelector("button");
  button.disabled = true; $("#login-error").textContent = "";
  try { const data = await api("/api/login", Object.fromEntries(new FormData(form))); form.reset(); signedIn(data); }
  catch (error) { $("#login-error").textContent = error.message; }
  finally { button.disabled = false; }
});
$("#logout").addEventListener("click", async () => {
  try { await api("/api/logout", {}); signedOut(); } catch (error) { notice(error.message, true); }
});
$("#refresh").addEventListener("click", render);
window.addEventListener("hashchange", () => { window.scrollTo(0, 0); render(); });
window.addEventListener("signed-out", signedOut);
window.addEventListener("reload-page", render);
api("/api/me").then(signedIn).catch(signedOut).finally(() => {
  $("#login-form button").disabled = false;
  document.documentElement.dataset.ready = "true";
});
