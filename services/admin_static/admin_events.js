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

const listText = value => (Array.isArray(value) ? value.join(", ") : String(value || "")) || "None";

// Steps through the chosen events one activation window at a time. Each item
// is {name, template_id, min_level, max_level, status_effects, progress_bars}
// plus event_id and config when it reactivates an existing instance; without
// event_id it starts a new run. Resolves once the dialog closes, with the
// number of runs activated.
export function activationWizard(items, revision) {
  const steps = items.map(item => ({item, state: "pending", settings: null, error: ""}));
  const dialog = document.createElement("dialog");
  dialog.id = "event-batch";
  dialog.setAttribute("aria-labelledby", "event-batch-title");
  document.body.append(dialog);
  let index = 0, reason = "", busy = false;

  const settingsFor = step => step.settings || {
    duration: "7", end: "",
    chance: Math.round(Number(step.item.config?.collectible_drop_chance ?? 0.35) * 1000) / 10,
    amount: Number(step.item.config?.collectible_amount ?? 1),
    participation: step.item.config?.participation_default ?? true
  };
  const remember = form => {
    steps[index].settings = {
      duration: form.elements.event_duration.value, end: form.elements.event_end.value,
      chance: form.elements.chance.value, amount: form.elements.amount.value,
      participation: form.elements.participation.checked
    };
    reason = form.elements.reason.value;
  };
  const label = {pending: "Pending", activated: "Activated", skipped: "Skipped"};

  function draw() {
    const step = steps[index], item = step.item, settings = settingsFor(step), done = step.state === "activated";
    const last = index === steps.length - 1;
    dialog.innerHTML = `<form id="event-batch-form" novalidate>
      <div class="batch-heading"><h2 id="event-batch-title">${esc(item.name || item.template_id)}</h2>
        <span class="batch-count">Event ${index + 1} of ${steps.length}</span></div>
      <ol class="batch-steps" aria-label="Selected events">${steps.map((entry, position) =>
        `<li class="${entry.state}${position === index ? " current" : ""}"><button type="button" class="text-button" data-step="${position}"
          title="${esc(entry.item.name || entry.item.template_id)} · ${label[entry.state]}">${position + 1}</button></li>`).join("")}</ol>
      ${facts([
        ["Template", `<code>${esc(item.template_id)}</code>`],
        ["Run", item.event_id ? `Reactivates instance ${esc(item.event_id)} (keeps player progress)` : "New run (fresh player progress)"],
        ["Levels", `${esc(item.min_level)}–${esc(item.max_level)}`],
        ["Status effects", esc(listText(item.status_effects))], ["Progress bars", esc(listText(item.progress_bars))]
      ])}
      ${done ? `<p class="notice">Activated${step.event_id ? ` as instance ${esc(step.event_id)}` : ""}.</p>` : `${endingControls()}
      <fieldset class="form-section"><legend>Run settings</legend>
        <div class="form-grid"><label>Collectible drop chance (%)<input name="chance" type="number" min="0" max="100" step="0.1" required value="${esc(settings.chance)}"><small>Applies to eligible monsters in the event's maps.</small></label>
          <label>Collectibles per drop<input name="amount" type="number" min="1" max="1000000" step="1" required value="${esc(settings.amount)}"></label></div>
        <label class="check"><input name="participation" type="checkbox" ${settings.participation ? "checked" : ""}>Players participate by default</label>
      </fieldset>
      <label>Reason<textarea name="reason" required minlength="3" maxlength="500" rows="2" placeholder="Support ticket or reason for this action">${esc(reason)}</textarea>
        <small>Recorded with your account in the audit log for each event activated here.</small></label>`}
      <p id="event-batch-error" class="error-text" role="alert">${esc(step.error)}</p>
      <div class="form-actions"><button type="button" class="secondary left" data-close>Close</button>
        <button type="button" class="secondary" data-move="-1" ${index === 0 ? "disabled" : ""}>Previous</button>
        ${done ? `<button type="button" data-move="1" ${last ? "disabled" : ""}>Next</button>` :
          `<button type="button" class="secondary" data-skip>${last ? "Skip" : "Skip to next"}</button>
           <button type="submit">${last ? "Activate" : "Activate and next"}</button>`}</div>
    </form>`;
    const form = dialog.querySelector("form");
    if (!done) {
      form.elements.event_duration.value = settings.duration;
      form.elements.event_end.value = settings.end;
      bindEnding(form);
    }
    const go = position => {
      if (busy) return;
      if (!done) remember(form);
      index = Math.max(0, Math.min(steps.length - 1, position)); draw();
    };
    form.querySelectorAll("[data-step]").forEach(button => button.addEventListener("click", () => go(Number(button.dataset.step))));
    form.querySelectorAll("[data-move]").forEach(button => button.addEventListener("click", () => go(index + Number(button.dataset.move))));
    form.querySelector("[data-close]").addEventListener("click", () => dialog.close());
    form.querySelector("[data-skip]")?.addEventListener("click", () => {
      steps[index].state = "skipped"; steps[index].error = "";
      if (last) dialog.close(); else go(index + 1);
    });
    form.addEventListener("submit", async action => {
      action.preventDefault();
      if (busy) return;
      remember(form);
      const error = form.querySelector("#event-batch-error"), buttons = [...form.querySelectorAll("button")];
      try {
        const ending = readEnding(form);
        const chance = Number(form.elements.chance.value), amount = Number(form.elements.amount.value);
        if (!form.elements.chance.value || !(chance >= 0 && chance <= 100)) throw new Error("Enter a drop chance from 0 to 100%.");
        if (!Number.isInteger(amount) || amount < 1 || amount > 1000000) throw new Error("Enter a whole number of collectibles from 1 to 1,000,000.");
        const text = reason.trim();
        if (text.length < 3 || text.length > 500) throw new Error("Enter a reason of 3 to 500 characters.");
        busy = true; buttons.forEach(button => button.disabled = true);
        const body = {revision, reason: text, ...ending, collectible_drop_chance: chance / 100,
          collectible_amount: amount, participation_default: form.elements.participation.checked};
        const response = item.event_id ? await api(`/api/events/${item.event_id}/activate`, body)
          : await api("/api/events", {...body, template_id: item.template_id});
        revision = response.revision;
        Object.assign(steps[index], {state: "activated", event_id: response.event_id, error: ""});
        notice(refreshMessage(`${item.name || item.template_id} activated.`, response));
        busy = false;
        const pending = steps.map((entry, position) => entry.state === "pending" ? position : -1).filter(position => position >= 0);
        const next = pending.find(position => position > index) ?? pending[0];
        if (next === undefined) dialog.close(); else go(next);
      } catch (failure) {
        busy = false;
        steps[index].error = failure.message;
        error.textContent = failure.message;
        buttons.forEach(button => button.disabled = false);
        // Another change moved the schedule on; pick up its revision so a
        // retry applies to what is saved now.
        try { revision = (await api("/api/events?limit=1")).revision; } catch { /* keep the old revision */ }
      }
    });
  }

  draw();
  dialog.showModal();
  const leave = () => dialog.open && dialog.close();
  window.addEventListener("hashchange", leave, {once: true});
  return new Promise(resolve => dialog.addEventListener("close", () => {
    window.removeEventListener("hashchange", leave);
    dialog.remove();
    const activated = steps.filter(step => step.state === "activated").length;
    if (activated > 1) notice(`${activated} of ${steps.length} selected events activated.`);
    resolve(activated);
  }, {once: true}));
}

export async function eventsPage(route) {
  const data = await api(`/api/events?${new URLSearchParams({limit: 50, ...route.params})}`);
  const filter = '<label for="event-state">Show</label><select id="event-state">' +
    [["", "All events"], ["active", "Active"], ["upcoming", "Upcoming"], ["expired", "Inactive"]]
      .map(([value, label]) => `<option value="${value}" ${route.params.state === value ? "selected" : ""}>${label}</option>`).join("") + '</select>';
  const running = new Set(data.rows.filter(event => event.state !== "expired").map(event => event.template_id));
  const selectable = event => event.manageable && event.state === "expired" && !running.has(event.template_id);
  const anySelectable = data.rows.some(selectable);
  return {
    title: "Events", description: "Manage active game events and retained event runs.", breadcrumbs: [["Administration", "status"], ["Events"]],
    actions: link("Add event", "events/new", {}, "button"),
    html: stats([["Active", data.counts.active], ["Upcoming", data.counts.upcoming], ["Inactive", data.counts.expired]]) +
      toolbar(route, "Search event name, template or ID", filter) +
      (anySelectable ? `<div class="selection batch-bar"><label class="check"><input type="checkbox" id="select-events">Select all inactive events on this page</label>
        <span><span id="selected-count" class="muted">None selected</span> <button type="button" id="activate-selected" disabled>Activate selected</button></span></div>` : "") +
      table(["Event", "Instance", "State", "Start", "End", "Levels", ""], data.rows.map(event => [
        (selectable(event) ? `<label class="row-select"><input type="checkbox" data-select-event="${event.event_id}" aria-label="Select ${esc(event.name)}"></label>` : "") +
        link(event.name, `events/${event.event_id}`, route.params) + `<span class="sub mono">${esc(event.template_id)}</span>` +
        (event.manageable && event.state === "expired" && running.has(event.template_id) ? '<span class="sub">Another run of this event is active</span>' : ""),
        event.event_id, status(stateLabel(event.state), event.state === "active" ? "good" : ""),
        esc(date(event.starts_at)), event.mode === "active" ? "Until deactivated" : esc(date(event.ends_at)),
        `${event.min_level}–${event.max_level}`,
        event.manageable && event.state !== "expired" ? `<button type="button" class="secondary" data-deactivate="${event.event_id}">Deactivate</button>` : ""
      ]), "No events match this search.") + pager(data, route) + refreshHelp,
    mount() {
      bindSearch(route);
      $("#event-state").addEventListener("change", event => navigate(route.path, {...route.params, state: event.target.value, offset: 0}));
      const boxes = [...document.querySelectorAll("[data-select-event]")];
      const update = () => {
        const count = boxes.filter(box => box.checked).length;
        $("#selected-count").textContent = count ? `${count} selected` : "None selected";
        $("#activate-selected").disabled = !count;
        $("#activate-selected").textContent = count > 1 ? `Activate ${count} events` : "Activate selected";
        $("#select-events").checked = count === boxes.length;
        $("#select-events").indeterminate = count > 0 && count < boxes.length;
      };
      boxes.forEach(box => box.addEventListener("change", update));
      $("#select-events")?.addEventListener("change", action => { boxes.forEach(box => box.checked = action.target.checked); update(); });
      $("#activate-selected")?.addEventListener("click", async () => {
        const chosen = boxes.filter(box => box.checked).map(box => data.rows.find(event => String(event.event_id) === box.dataset.selectEvent));
        // One run per template can be active; keep the newest selected run.
        const seen = new Set(), items = chosen.filter(event => !seen.has(event.template_id) && seen.add(event.template_id));
        if (items.length < chosen.length) notice("Only one run per event can be active; extra runs of the same event were left out.", true);
        if (await activationWizard(items, data.revision)) window.dispatchEvent(new Event("reload-page"));
      });
      for (const button of document.querySelectorAll("[data-deactivate]")) button.addEventListener("click", async () => {
        const event = data.rows.find(row => String(row.event_id) === button.dataset.deactivate);
        const reason = await confirmAction("Deactivate event?", `${event.name} / Instance ${event.event_id}. Removes this run from active events; player progress and claimed rewards are kept.`, "Deactivate");
        if (reason === null) return;
        button.disabled = true;
        try {
          const response = await api(`/api/events/${event.event_id}/deactivate`, {revision: data.revision, reason});
          notice(refreshMessage(`${event.name} deactivated.`, response));
          window.dispatchEvent(new Event("reload-page"));
        } catch (error) { button.disabled = false; notice(error.message, true); }
      });
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
  return {
    title: "Add event", description: "Start new active event runs from the game catalog.",
    breadcrumbs: [["Events", "events"], ["Add event"]],
    html: `<form id="new-event" class="form-page">
      <fieldset class="form-section"><legend>Event templates</legend>
        <label for="template-search">Find event</label><div class="toolbar"><input id="template-search" placeholder="Event name or template ID" maxlength="100"><button type="button" id="find-template" class="secondary">Search templates</button></div>
        <div id="template-results"></div>
        <p class="help">Choose one or more global game events; selections are kept across searches. Map-local scripts and client-owned events are managed by the game.</p>
      </fieldset>
      <fieldset class="form-section"><legend>Selected events</legend><div id="selected-templates"></div>
        <p class="muted">Next, each event gets its own activation window for its end date and run settings. New runs start immediately with fresh player progress and reward claims.</p>
      </fieldset>
      ${refreshHelp}
      <p id="form-error" class="error-text" role="alert"></p><div class="form-actions">${link("Cancel", "events", {}, "left")}<button id="add-event" type="submit" disabled>Continue</button></div>
    </form>`,
    mount() {
      const form = $("#new-event"), selected = new Map(); let generation = 0, results = [];
      const toggle = template => `<button type="button" class="secondary" data-template="${esc(template.template_id)}">${selected.has(template.template_id) ? "Remove" : "Select"}</button>`;
      function drawSelection() {
        $("#selected-templates").innerHTML = table(["Event", "Levels", ""], [...selected.values()].map(template => [
          `${esc(template.name || template.template_id)}<span class="sub mono">${esc(template.template_id)}</span>`,
          `${template.min_level}–${template.max_level}`, toggle(template)
        ]), "No events selected yet.");
        for (const button of document.querySelectorAll("#template-results [data-template]")) button.textContent = selected.has(button.dataset.template) ? "Remove" : "Select";
        $("#add-event").disabled = !selected.size;
        $("#add-event").textContent = selected.size > 1 ? `Continue with ${selected.size} events` : "Continue";
      }
      form.addEventListener("click", action => {
        const button = action.target.closest("[data-template]");
        if (!button) return;
        const id = button.dataset.template;
        if (selected.has(id)) selected.delete(id);
        else selected.set(id, results.find(template => template.template_id === id));
        drawSelection();
      });
      async function search() {
        const version = ++generation, button = $("#find-template"); button.disabled = true;
        try {
          const data = await api(`/api/event-templates?limit=30&q=${encodeURIComponent($("#template-search").value)}`);
          if (!form.isConnected || version !== generation) return;
          results = data.rows;
          $("#template-results").innerHTML = table(["Event", "Levels", ""], data.rows.map(template => [
            `${esc(template.name || template.template_id)}<span class="sub mono">${esc(template.template_id)}</span>`,
            `${template.min_level}–${template.max_level}`, toggle(template)
          ]), "No game events match this search.") + (data.total > data.rows.length ? '<p class="help">Refine the search to see more templates.</p>' : "");
        } catch (error) { if (form.isConnected) formError(error); } finally { button.disabled = false; }
      }
      $("#find-template").addEventListener("click", search);
      $("#template-search").addEventListener("keydown", action => { if (action.key === "Enter") { action.preventDefault(); search(); } });
      drawSelection();
      form.addEventListener("submit", async action => {
        action.preventDefault(); if (!selected.size) return;
        const button = $("#add-event"); button.disabled = true;
        try {
          const {revision} = await api("/api/events?limit=1");
          const activated = await activationWizard([...selected.values()], revision);
          if (activated) navigate("events", {state: "active"});
        } catch (error) { if (form.isConnected) formError(error); } finally { if (form.isConnected) button.disabled = !selected.size; }
      });
    }
  };
}
