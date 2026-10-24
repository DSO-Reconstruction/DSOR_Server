import { $, esc, date, api, link, navigate, status, table, facts, stats, section, toolbar,
  bindSearch, pager, formError, notice, confirmAction, tree } from "./admin_ui.js";

const stateLabel = state => ({active: "Active", upcoming: "Upcoming", expired: "Inactive"}[state] || state);
export const refreshMessage = (action, result) => `${action} ${result.refresh.updated} online ${result.refresh.updated === 1 ? "character" : "characters"} updated.${result.refresh.deferred ? ` ${result.refresh.deferred} could not refresh now; their next map entry will load the change.` : ""}`;
const refreshHelp = '<p class="help">Online event state, drops and buffs refresh immediately. Event NPCs and scenery load on map entry; existing shared map instances may need to be recreated.</p>';

export function endingControls(event = null) {
  const selected = event ? (event.ends_at && event.mode !== "active" ? "custom" : "none") : "7";
  const end = event?.ends_at ? new Date(event.ends_at) : null;
  const localEnd = end ? new Date(end.getTime() - end.getTimezoneOffset() * 60000).toISOString().slice(0, 19) : "";
  return `<fieldset class="form-section event-ending"><legend>Event ends</legend>
    <div class="duration-options">${[["3", "3d"], ["7", "7d"], ["14", "14d"], ["31", "31d"], ["custom", "Custom date"], ["none", "Until deactivated"]].map(([value, label]) =>
      `<label><input type="radio" name="event_duration" value="${value}" ${selected === value ? "checked" : ""}><span>${label}</span></label>`).join("")}</div>
    <label data-custom-end ${selected !== "custom" ? "hidden" : ""}>End date and time<input name="event_end" type="datetime-local" step="1" value="${esc(localEnd)}" ${selected === "custom" ? "required" : "disabled"}></label>
    <p class="help" data-end-preview aria-live="polite"></p>
    <p class="help">Times use ${esc(Intl.DateTimeFormat().resolvedOptions().timeZone)}. Each day is 24 hours, measured from ${event ? "saving this change" : "activation"}.</p>
  </fieldset>`;
}

export function bindEnding(form) {
  const update = () => {
    const choice = form.elements.event_duration.value, input = form.elements.event_end;
    form.querySelector("[data-custom-end]").hidden = choice !== "custom";
    input.disabled = choice !== "custom"; input.required = choice === "custom";
    const preview = form.querySelector("[data-end-preview]");
    if (choice === "none") preview.textContent = "Stays active until an administrator deactivates it.";
    else if (choice === "custom") preview.textContent = Number.isFinite(new Date(input.value).getTime()) ? `Ends ${date(new Date(input.value).toISOString())}.` : "Choose a future end date and time.";
    else preview.textContent = `Ends approximately ${date(Date.now() / 1000 + Number(choice) * 86400)}.`;
  };
  form.querySelector(".event-ending").addEventListener("input", update);
  update();
}

export function readEnding(form) {
  const choice = form.elements.event_duration.value;
  if (choice === "none") return {ends_at: null};
  if (choice !== "custom") return {duration_days: Number(choice)};
  const end = new Date(form.elements.event_end.value);
  if (!Number.isFinite(end.getTime()) || end.getTime() <= Date.now()) throw new Error("Choose a future end date and time.");
  return {ends_at: end.toISOString()};
}

export function endingDescription(ending) {
  return ending.duration_days ? `Runs for ${ending.duration_days} days.` : ending.ends_at ? `Ends ${date(ending.ends_at)}.` : "Stays active until deactivated.";
}

export async function eventsPage(route) {
  const data = await api(`/api/events?${new URLSearchParams({limit: 50, ...route.params})}`);
  const filter = '<label for="event-state">Show</label><select id="event-state">' +
    [["", "All events"], ["active", "Active"], ["upcoming", "Upcoming"], ["expired", "Inactive"]]
      .map(([value, label]) => `<option value="${value}" ${route.params.state === value ? "selected" : ""}>${label}</option>`).join("") + '</select>';
  return {
    title: "Events", description: "Manage active game events and retained event runs.", breadcrumbs: [["Administration", "status"], ["Events"]],
    actions: link("Add event", "events/new", {}, "button"),
    html: stats([["Active", data.counts.active], ["Upcoming", data.counts.upcoming], ["Inactive", data.counts.expired]]) +
      toolbar(route, "Search event name, template or ID", filter) +
      table(["Event", "Instance", "State", "Start", "End", "Levels"], data.rows.map(event => [
        link(event.name, `events/${event.event_id}`, route.params) + `<span class="sub mono">${esc(event.template_id)}</span>`,
        event.event_id, status(stateLabel(event.state), event.state === "active" ? "good" : ""),
        esc(date(event.starts_at)), event.mode === "active" ? "Until deactivated" : esc(date(event.ends_at)),
        `${event.min_level}–${event.max_level}`
      ]), "No events match this search.") + pager(data, route) + refreshHelp,
    mount() {
      bindSearch(route);
      $("#event-state").addEventListener("change", event => navigate(route.path, {...route.params, state: event.target.value, offset: 0}));
    }
  };
}

export async function eventPage(route) {
  const event = await api(`/api/events/${route.parts[1]}`), enable = event.state === "expired";
  const verb = enable ? "Activate" : "Deactivate";
  const result = {
    title: event.name, description: `${event.template_id} · Event instance ${event.event_id}`,
    breadcrumbs: [["Events", "events", route.params], [`#${event.event_id}`]],
    html: '<div class="columns">' + section("Event", facts([
      ["State", status(stateLabel(event.state), event.state === "active" ? "good" : "")],
      ["Instance ID", event.event_id], ["Template", `<code>${esc(event.template_id)}</code>`],
      ["Levels", `${event.min_level}–${event.max_level}`], ["Start", esc(date(event.starts_at))],
      ["End", event.mode === "active" ? "Until deactivated" : esc(date(event.ends_at))]
    ])) + section("Content", facts([
      ["Event map", esc(event.map_id || "Defined by event travel items")],
      ["Travel destination", esc(event.teleport_target || "None")],
      ["Status effects", esc(event.status_effects.join(", ") || "None")],
      ["Progress bars", esc(event.progress_bars.join(", ") || "None")]
    ])) + '</div>' + section("Run configuration", tree(event.config))
  };
  if (event.manageable) {
    const hasEnding = enable || event.state === "active";
    result.html += `<form id="event-action" class="form-page"><h2>${enable ? "Activate event" : "Manage event"}</h2>
      <p class="muted">${enable ? "Reactivating keeps this instance's player progress, participation and claimed rewards. Use Add event to start a fresh run." : "Changing the end date or deactivating keeps this instance's player progress, participation and claimed rewards."}</p>
      ${hasEnding ? endingControls(enable ? null : event) : ""}
      ${refreshHelp}<p id="form-error" class="error-text" role="alert"></p>
      <div class="form-actions">${event.state === "active" ? '<button type="submit" value="schedule">Save end date</button>' : ""}
      <button type="submit" value="${enable ? "activate" : "deactivate"}" ${enable ? "" : 'class="secondary" formnovalidate'}>${verb} event</button></div></form>`;
    result.mount = () => {
      const form = $("#event-action");
      if (hasEnding) bindEnding(form);
      form.addEventListener("submit", async action => {
        action.preventDefault();
        const operation = action.submitter?.value || (enable ? "activate" : event.state === "active" ? "schedule" : "deactivate");
        const buttons = [...form.querySelectorAll("button")]; buttons.forEach(button => button.disabled = true);
        try {
          const ending = operation === "deactivate" ? {} : readEnding(form);
          const label = operation === "schedule" ? "Save end date" : verb;
          const reason = await confirmAction(`${label}?`, `${event.name} / Instance ${event.event_id}. ${operation === "deactivate" ? "Removes this run from active events." : endingDescription(ending)}`, label);
          if (reason === null) return;
          const response = await api(`/api/events/${event.event_id}/${operation}`, {
            revision: event.revision, reason, ...ending
          });
          notice(refreshMessage(operation === "schedule" ? "End date updated." : `Event ${enable ? "activated" : "deactivated"}.`, response));
          window.dispatchEvent(new Event("reload-page"));
        } catch (error) { if (form.isConnected) formError(error); } finally { buttons.forEach(button => button.disabled = false); }
      });
    };
  } else result.html += '<p class="muted">This map-local or client-owned event is managed by the game.</p>';
  return result;
}

export async function newEventPage() {
  const {revision} = await api("/api/events?limit=1");
  return {
    title: "Add event", description: "Start a new active event run from the game catalog.",
    breadcrumbs: [["Events", "events"], ["Add event"]],
    html: `<form id="new-event" class="form-page">
      <fieldset class="form-section"><legend>Event template</legend>
        <label for="template-search">Find event</label><div class="toolbar"><input id="template-search" placeholder="Event name or template ID" maxlength="100"><button type="button" id="find-template" class="secondary">Search templates</button></div>
        <div id="template-results"></div><div id="selected-template"></div>
        <p class="help">Choose a global game event. Map-local scripts and client-owned events are managed by the game.</p>
      </fieldset>
      ${endingControls()}
      <fieldset class="form-section"><legend>Run settings</legend>
        <p class="muted">Starts immediately. A new instance starts fresh player progress and reward claims.</p>
        <div class="form-grid"><label>Collectible drop chance (%)<input name="chance" type="number" min="0" max="100" step="0.1" value="35" required><small>Applies to eligible monsters and maps defined for the event.</small></label>
          <label>Collectibles per drop<input name="amount" type="number" min="1" max="1000000" step="1" value="1" required></label></div>
        <label class="check"><input name="participation" type="checkbox" checked>Players participate by default</label>
      </fieldset>
      ${refreshHelp}
      <p id="form-error" class="error-text" role="alert"></p><div class="form-actions">${link("Cancel", "events", {}, "left")}<button id="add-event" type="submit" disabled>Activate event</button></div>
    </form>`,
    mount() {
      const form = $("#new-event"); let selected = null, generation = 0;
      bindEnding(form);
      async function search() {
        const version = ++generation, button = $("#find-template"); button.disabled = true;
        try {
          const data = await api(`/api/event-templates?limit=30&q=${encodeURIComponent($("#template-search").value)}`);
          if (!form.isConnected || version !== generation) return;
          $("#template-results").innerHTML = table(["Event", "Levels", ""], data.rows.map(template => [
            `${esc(template.name || template.template_id)}<span class="sub mono">${esc(template.template_id)}</span>`,
            `${template.min_level}–${template.max_level}`, `<button type="button" class="secondary" data-template="${esc(template.template_id)}">Select</button>`
          ]), "No game events match this search.") + (data.total > data.rows.length ? '<p class="help">Refine the search to see more templates.</p>' : "");
          for (const button of $("#template-results").querySelectorAll("[data-template]")) button.addEventListener("click", () => {
            selected = data.rows.find(template => template.template_id === button.dataset.template);
            $("#selected-template").innerHTML = section("Selected event", facts([
              ["Name", esc(selected.name)], ["Template", `<code>${esc(selected.template_id)}</code>`],
              ["Status effects", esc(selected.status_effects || "None")], ["Progress bars", esc(selected.progress_bars || "None")]
            ]));
            $("#add-event").disabled = false;
          });
        } catch (error) { if (form.isConnected) formError(error); } finally { button.disabled = false; }
      }
      $("#find-template").addEventListener("click", search);
      $("#template-search").addEventListener("keydown", action => { if (action.key === "Enter") { action.preventDefault(); search(); } });
      form.addEventListener("submit", async action => {
        action.preventDefault(); if (!selected) return;
        const values = new FormData(form), template = selected, button = $("#add-event"); button.disabled = true;
        try {
          const ending = readEnding(form);
          const reason = await confirmAction("Activate a new event run?", `${template.name || template.template_id} will start immediately. ${endingDescription(ending)}`, "Activate event");
          if (reason === null) return;
          const response = await api("/api/events", {revision, template_id: template.template_id, ...ending,
            collectible_drop_chance: Number(values.get("chance")) / 100, collectible_amount: Number(values.get("amount")),
            participation_default: values.has("participation"), reason});
          notice(refreshMessage("Event activated.", response)); navigate(`events/${response.event_id}`);
        } catch (error) { if (form.isConnected) formError(error); } finally { button.disabled = false; }
      });
    }
  };
}
