import { $, esc, date, api, link, navigate, status, table, facts, stats, section, tabs,
  formError, notice, confirmAction } from "./admin_ui.js";
import { endingControls, bindEnding, readEnding, endingDescription, refreshMessage } from "./admin_events.js";

const tracks = [["free", "Free"], ["gold", "Gold ticket"], ["red", "Red ticket"]];
const stateLabel = state => ({active: "Running", upcoming: "Upcoming", expired: "Ended"}[state] || state);
const pageTabs = active => tabs([["Season", "season-pass"], ["Rewards", "season-pass/rewards"], ["Missions", "season-pass/missions"]], active);
const reward = track => `${esc(track.name)} × ${Number(track.amount).toLocaleString()}<span class="sub mono">${esc(track.item_id)}</span>`;

function databaseMessage(databases) {
  if (!databases?.length) return "";
  const failed = databases.filter(d => d.error), changed = databases.filter(d => d.changed);
  if (failed.length) return ` ${failed.length} client database${failed.length === 1 ? "" : "s"} could not be updated: ${failed.map(d => `${d.path} (${d.error})`).join("; ")}.`;
  return changed.length ? ` Written to ${changed.length} client database${changed.length === 1 ? "" : "s"}; restart those clients to see the change.` : " Client databases already match.";
}

function databaseSection(databases) {
  const rows = databases.map(d => [`<code class="resource">${esc(d.path)}</code>`,
    d.error ? status(d.error, "bad") : d.in_sync ? status("Matches the server", "good") : status(`${d.differences} levels differ`, "bad")]);
  const help = '<p class="help">The game client draws the reward tiles and level thresholds from its own static.db4, so edits are written into these client copies as well (restart the client afterwards). Set <code>DSO_CLIENT_STATIC_DB</code> to the client\'s <code>export_win32\\db\\static.db4</code>; separate several paths with <code>;</code>. dsor clients download the edited table from the account service automatically.</p>';
  if (!databases.length) return section("Client databases", '<p class="muted">No client databases are configured, so clients keep showing their original rewards.</p>' + help);
  return section("Client databases", table(["Database", "Rewards"], rows) + help +
    `<p id="sync-error" class="error-text" role="alert"></p><div class="form-actions"><button id="sync-clients" type="button" class="secondary">Write rewards to client databases</button></div>`);
}

function bindSync() {
  $("#sync-clients")?.addEventListener("click", async event => {
    const button = event.target; button.disabled = true;
    try {
      const reason = await confirmAction("Write rewards to client databases?", "Copies the server's Season Pass reward table into every configured client static.db4. A backup is kept beside each file.", "Write rewards");
      if (reason === null) return;
      const result = await api("/api/season-pass/sync", {reason});
      notice("Client databases updated." + databaseMessage(result.databases), result.databases.some(d => d.error));
      window.dispatchEvent(new Event("reload-page"));
    } catch (error) { $("#sync-error").textContent = error.message; } finally { button.disabled = false; }
  });
}

async function seasonTab(result) {
  const data = await api("/api/season-pass"), current = data.current, running = current?.state === "active";
  const last = data.runs[0], resumable = !current && last && last.manageable !== false;
  result.description = `${data.name} · ${data.levels} levels · ${data.missions} missions`;
  result.html += stats([["Season", current ? stateLabel(current.state) : "Not running"],
    ["Ends", running ? (current.mode === "active" ? "When ended" : date(current.ends_at)) : "—"],
    ["Characters this season", running ? data.players.in_season : "—"], ["Average level", running ? data.players.average_level : "—"],
    [`Reached level ${data.levels}`, running ? data.players.max_level : "—"]]);
  let form;
  if (!data.manageable) {
    form = `<p class="error-text">The Season Pass event <code>${esc(data.template_id)}</code> is not a global event in this static database, so seasons cannot be scheduled here.</p>`;
  } else if (current) {
    form = `<form id="season-form" class="form-page">${facts([["Season", link(`#${current.event_id}`, `events/${current.event_id}`)],
      ["State", status(stateLabel(current.state), running ? "good" : "")], ["Started", esc(date(current.starts_at))],
      ["Ends", current.mode === "active" ? "When ended" : esc(date(current.ends_at))]])}
      ${running ? endingControls(current) : ""}
      <p id="form-error" class="error-text" role="alert"></p>
      <div class="form-actions"><button type="submit" value="deactivate" class="secondary" formnovalidate>End season now</button>${running ? '<button type="submit" value="schedule">Save end date</button>' : ""}</div></form>`;
  } else {
    form = `<form id="season-form" class="form-page">
      <p class="muted">No season is running: the client hides its Season Pass window and missions do not count. Starting a season begins a new run of <code>${esc(data.template_id)}</code> immediately.</p>
      ${endingControls()}
      <p class="help">A new season starts every character at level 0 with no mission progress or claimed rewards; activated Gold and Red tickets carry over. Progress saved before seasons were tracked joins the first season it meets.${resumable ? ` Resuming season #${last.event_id} keeps everyone's progress from it.` : ""}</p>
      <p id="form-error" class="error-text" role="alert"></p>
      <div class="form-actions">${resumable ? `<button type="submit" value="activate" class="secondary">Resume season #${last.event_id}</button>` : ""}<button type="submit" value="start">Start new season</button></div></form>`;
  }
  result.html += section("Season lifespan", form);
  result.html += section("Seasons", table(["Season", "State", "Started", "Ended"], data.runs.map(run => [
    link(`#${run.event_id}`, `events/${run.event_id}`), status(stateLabel(run.state), run.state === "active" ? "good" : ""),
    esc(date(run.starts_at)), run.mode === "active" ? "When ended" : esc(date(run.ends_at))]), "No season has been scheduled yet."));
  result.html += section("What the client receives", `<p class="muted">The client runs its Season Pass while the event named by <code>_Globals.SeasonPassEventId</code> (<code>${esc(data.template_id)}</code>) is active. Its start and end reach the client in <code>EventUpdate</code> at map entry and whenever a season starts or ends here; the countdown uses that end time.</p>
    <p class="muted">Level, experience, activated tickets and claimed rewards travel in <code>GetSeasonPassRewardsInfoResultCommand</code>, mission progress in <code>GetSeasonPassMissionsInfoResultCommand</code>. Both answer the client's queries and are resent when progress changes or a season starts or ends. Reward contents and level thresholds are not sent: the client reads them from its static.db4.</p>`);
  result.html += databaseSection(data.databases);
  result.mount = () => {
    bindSync();
    const formElement = $("#season-form");
    if (!formElement) return;
    // Seasons usually run for weeks; offer the longest preset for a new one.
    if (!current) formElement.querySelector('input[name="event_duration"][value="31"]')?.click();
    if (formElement.querySelector(".event-ending")) bindEnding(formElement);
    formElement.addEventListener("submit", async action => {
      action.preventDefault();
      const operation = action.submitter?.value || (current ? "schedule" : "start");
      const buttons = [...formElement.querySelectorAll("button")]; buttons.forEach(button => button.disabled = true);
      try {
        const ending = operation === "deactivate" ? {} : readEnding(formElement);
        const [title, message, label] = {
          start: ["Start a new season?", `Characters start the new season at level 0. ${endingDescription(ending)}`, "Start season"],
          activate: [`Resume season #${last?.event_id}?`, `Characters keep their progress from this season. ${endingDescription(ending)}`, "Resume season"],
          schedule: ["Change the season end?", endingDescription(ending), "Save end date"],
          deactivate: ["End the season now?", "The client hides the Season Pass and missions stop counting. Progress is kept until a new season starts.", "End season"],
        }[operation];
        const reason = await confirmAction(title, message, label);
        if (reason === null) return;
        const response = operation === "start"
          ? await api("/api/events", {revision: data.revision, template_id: data.template_id, participation_default: true, ...ending, reason})
          : await api(`/api/events/${(current || last).event_id}/${operation}`, {revision: data.revision, ...ending, reason});
        notice(refreshMessage({start: "Season started.", activate: "Season resumed.", schedule: "Season end updated.", deactivate: "Season ended."}[operation], response));
        window.dispatchEvent(new Event("reload-page"));
      } catch (error) { if (formElement.isConnected) formError(error); } finally { buttons.forEach(button => button.disabled = false); }
    });
  };
  return result;
}

async function rewardsTab(result) {
  const data = await api("/api/season-pass/rewards");
  result.description = `${data.rows.length} levels · ${data.reward_set === "alternate" ? "2026 alternate reward set" : "standard reward set"}`;
  result.html += '<p class="help">The Gold track needs a Gold ticket (pass type 1, or 3 for double rewards), the Red track a Red ticket (2, or 4 for double). Experience is the total needed to reach the level. Edited values are marked; the rest come from static.db4.</p>';
  result.html += table(["Level", "Experience", ...tracks.map(([, label]) => label), ""], data.rows.map(row => [
    `<strong>${row.level}</strong>`,
    Number(row.exp).toLocaleString() + (row.exp !== row.static_exp ? '<span class="edited-mark">edited</span>' : ""),
    ...tracks.map(([key]) => reward(row.tracks[key]) + (row.tracks[key].edited ? '<span class="edited-mark">edited</span>' : "")),
    link("Edit", `season-pass/rewards/${row.level}`, {}, "button secondary")
  ]));
  result.html += databaseSection(data.databases);
  result.mount = bindSync;
  return result;
}

async function rewardPage(route, result) {
  const level = Number(route.parts[2]);
  const data = await api("/api/season-pass/rewards");
  const index = data.rows.findIndex(row => row.level === level);
  if (index < 0) throw new Error("Season Pass level not found.");
  const row = data.rows[index], below = data.rows[index - 1], above = data.rows[index + 1];
  result.title = `Season Pass level ${level}`;
  result.description = row.edited ? "Edited in the CMS" : "Values from static.db4";
  result.breadcrumbs = [["Season Pass", "season-pass"], ["Rewards", "season-pass/rewards"], [`Level ${level}`]];
  result.html += `<form id="reward-form" class="form-page">
    <fieldset class="form-section"><legend>Experience</legend><div class="form-grid">
      <label>Total experience for level ${level}<input name="exp" type="number" min="${below ? below.exp + 1 : 0}" max="${above ? above.exp - 1 : 4294967295}" step="1" value="${row.exp}" required>
        <small>${below ? `Level ${below.level} needs ${below.exp.toLocaleString()}` : "First level"}${above ? ` · level ${above.level} needs ${above.exp.toLocaleString()}` : ""} · static.db4: ${row.static_exp.toLocaleString()}</small></label>
    </div></fieldset>
    <fieldset class="form-section"><legend>Rewards</legend><div class="season-tracks">${tracks.map(([key, label]) => `<div>
      <h3>${label}</h3>
      <label>Item template ID<input name="${key}_item" value="${esc(row.tracks[key].item_id)}" required maxlength="200" autocomplete="off" spellcheck="false"></label>
      <label>Amount<input name="${key}_amount" type="number" min="1" max="1000000" step="1" value="${row.tracks[key].amount}" required></label>
      <p class="help">static.db4: ${esc(row.tracks[key].static.name)} × ${row.tracks[key].static.amount.toLocaleString()}</p></div>`).join("")}</div>
      <h3>Find an item</h3>
      <div class="toolbar"><input id="reward-item-search" aria-label="Find item" placeholder="In-game name or template ID" maxlength="100"><button id="reward-item-find" type="button" class="secondary">Find item</button></div>
      <div id="reward-item-results" class="picker-results"></div>
      <p class="help">Currency templates (such as <code>season_coin</code>) are credited to the wallet; other items go to the inventory at the character's level.</p>
    </fieldset>
    <p id="form-error" class="error-text" role="alert"></p>
    <div class="form-actions">${link("Back to rewards", "season-pass/rewards", {}, "left")}${row.edited ? '<button type="submit" value="reset" class="secondary">Reset to static.db4</button>' : ""}<button type="submit" value="save">Save level ${level}</button></div>
  </form>` + databaseSection(data.databases);
  result.mount = () => {
    bindSync();
    const form = $("#reward-form");
    async function findItems() {
      const button = $("#reward-item-find"); button.disabled = true;
      try {
        const found = await api(`/api/items?${new URLSearchParams({q: $("#reward-item-search").value.trim(), limit: 15})}`);
        if (!form.isConnected) return;
        $("#reward-item-results").innerHTML = table(["Item", "Template", "Category", "Use for"], found.rows.map(item => [
          esc(item.name), `<code class="resource">${esc(item.template_id)}</code>`, esc(item.category),
          tracks.map(([key, label]) => `<button type="button" class="secondary" data-track="${key}" data-item="${esc(item.template_id)}">${label.split(" ")[0]}</button>`).join(" ")
        ]), "No item templates matched.");
        for (const select of $("#reward-item-results").querySelectorAll("[data-track]")) select.addEventListener("click", () => {
          form.elements[`${select.dataset.track}_item`].value = select.dataset.item;
        });
      } catch (error) { formError(error); } finally { button.disabled = false; }
    }
    $("#reward-item-find").addEventListener("click", findItems);
    $("#reward-item-search").addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); findItems(); } });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const reset = event.submitter?.value === "reset";
      if (!reset && !form.reportValidity()) return;
      const buttons = [...form.querySelectorAll("button")]; buttons.forEach(button => button.disabled = true);
      try {
        const values = new FormData(form);
        const payload = reset ? {reset: true} : {exp: Number(values.get("exp")), ...Object.fromEntries(tracks.map(([key]) =>
          [key, {item_id: String(values.get(`${key}_item`)).trim(), amount: Number(values.get(`${key}_amount`))}]))};
        const summary = reset ? "Restore the static.db4 experience and rewards." :
          tracks.map(([key, label]) => `${label}: ${payload[key].item_id} × ${payload[key].amount}`).join(" · ") + ` · ${payload.exp.toLocaleString()} experience.`;
        const reason = await confirmAction(reset ? `Reset level ${level}?` : `Save level ${level}?`, summary, reset ? "Reset level" : "Save level");
        if (reason === null) return;
        const response = await api(`/api/season-pass/rewards/${level}`, {...payload, revision: data.revision, reason});
        notice(`Level ${level} saved; claims use it immediately.` + databaseMessage(response.databases), response.databases.some(d => d.error));
        navigate("season-pass/rewards");
      } catch (error) { if (form.isConnected) formError(error); } finally { buttons.forEach(button => button.disabled = false); }
    });
  };
  return result;
}

async function missionsTab(result) {
  const data = await api("/api/season-pass/missions");
  result.description = `${data.rows.length} missions from static.db4`;
  result.html += '<p class="help">Missions count only while a season runs. Completing one adds its experience once per season.</p>' +
    table(["Mission", "Category", "Objective", "Target", "Condition", "Experience"], data.rows.map(m => [
      esc(m.mission_id), esc(m.category), esc(m.objective), Number(m.target).toLocaleString(),
      [m.special && `<code>${esc(m.special)}</code>`, m.difficulty && m.difficulty !== "Any" && `Difficulty ${esc(m.difficulty)}+`].filter(Boolean).join(" · ") || "—",
      Number(m.exp).toLocaleString()]), "This static database has no Season Pass missions.");
  return result;
}

export async function seasonPassPage(route) {
  const tab = route.parts[1] || "season";
  const result = {title: "Season Pass", description: "", breadcrumbs: [["Administration", "status"], ["Season Pass"]],
    html: pageTabs(tab === "season" ? "season-pass" : `season-pass/${tab}`)};
  if (tab === "season") return seasonTab(result);
  if (tab === "rewards" && route.parts[2]) return rewardPage(route, result);
  if (tab === "rewards") return rewardsTab(result);
  if (tab === "missions") return missionsTab(result);
  throw new Error("Season Pass page not found.");
}
